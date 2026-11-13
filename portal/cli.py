from datetime import timedelta
import os
from time import sleep

import click
from pymongo.errors import PyMongoError

from .accounts import Auth0Management, link_existing_user, provision, username_field
from .billing import deliver_next, refresh_overdue
from .core import ROLES, Problem, atomic, boundary, db, email_field, ensure_indexes, lock_accounts, now, today
from .recurring import maintain_series
from .lesson_reminders import enqueue_reminders, deliver_next_reminder


def seed_records(tutor, parent, student):
    tutor_id, parent_id, student_id = (user["_id"] for user in (tutor, parent, student))

    def seed(transaction):
        database = db()
        lock_accounts(transaction, {tutor_id: "tutor", parent_id: "parent", student_id: "student"})
        if database.demo_fixtures.find_one({"_id": student_id}, session=transaction):
            return

        database.users.update_one(
            {"_id": student_id}, {"$set": {"parent_id": parent_id}}, session=transaction,
        )
        database.users.update_one({"_id": tutor_id}, {"$set": {"billing": {
            "name": "Demo Tutoring", "abn": "10000000000", "email": tutor["email"],
            "address": "Sydney, NSW", "phone": "", "payment": "Demo invoice for testing.", "due_days": 14,
        }}}, session=transaction)
        timestamp = now()
        database.enrollments.update_one(
            {"tutor_id": tutor_id, "student_id": student_id},
            {"$setOnInsert": {"rate_cents": 6500, "weekly_target": 2, "created_at": timestamp}},
            upsert=True, session=transaction,
        )
        database.weekly_availability.insert_many([
            {owner_key: owner_id, "weekday": day, "start_min": 540, "end_min": 1140}
            for owner_key, owner_id in (("tutor_id", tutor_id), ("parent_id", parent_id))
            for day in range(7)
        ], session=transaction)
        local_day = today()
        next_lesson = boundary(local_day + timedelta(days=1), 960)
        past_lesson = boundary(local_day - timedelta(days=1), 960)
        common = {"tutor_id": tutor_id, "student_id": student_id, "rate_cents": 6500,
                  "invoice_id": None, "created_at": timestamp, "title": "Mathematics"}
        database.lessons.insert_many([
            {**common, "status": "scheduled", "start_at": next_lesson,
             "end_at": next_lesson + timedelta(hours=1)},
            {**common, "status": "completed", "start_at": past_lesson,
             "end_at": past_lesson + timedelta(hours=1), "charge": True, "billing_parent_id": parent_id,
             "finalized_at": timestamp,
             "parent_feedback": "We practised comparing fractions. Please encourage your child to show their working in the homework.",
             "student_feedback": "Good work explaining how you compared the fractions."},
        ], session=transaction)
        database.homework.insert_one({
            "tutor_id": tutor_id, "student_id": student_id, "title": "Practice comparing fractions",
            "instructions": "Show your working for three pairs of fractions. Upload photos or a document with your answers, then mark the homework complete.",
            "due_on": (local_day + timedelta(days=3)).isoformat(), "created_at": timestamp,
        }, session=transaction)
        database.demo_fixtures.insert_one({"_id": student_id, "created_at": timestamp}, session=transaction)

    atomic(seed)


def register_commands(app):
    @app.cli.command("init-db")
    def init_db():
        """Create the MongoDB indexes."""
        ensure_indexes()
        click.echo("MongoDB indexes are ready.")

    @app.cli.command("bootstrap-admin")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt=True)
    def bootstrap_admin(email, name):
        """Provision the initial Auth0 administrator and print a one-use setup link."""
        ensure_indexes()
        try:
            if not 1 <= len(name.strip()) <= 100:
                raise Problem("Enter an administrator name of up to 100 characters.")
            management = Auth0Management()
            account = provision(name.strip(), email_field({"email": email}), "admin", management=management, reuse=True)
            link = management.setup_link(account["auth0_sub"])
        except Problem as error:
            raise click.ClickException(error.message) from None
        click.echo(f"Setup link for {account['email']} (valid for 24 hours):")
        click.echo(link)

    @app.cli.command("link-auth0-user")
    @click.option("--email", required=True)
    @click.option("--name", required=True)
    @click.option("--role", type=click.Choice(ROLES), required=True)
    @click.option("--auth0-user-id", default=None, help="Select one identity if multiple Auth0 users match the email.")
    def link_auth0_user(email, name, role, auth0_user_id):
        """Grant portal access to an existing Auth0 user, preserving their password."""
        ensure_indexes()
        try:
            if not 1 <= len(name.strip()) <= 100:
                raise Problem("Enter a name of up to 100 characters.")
            account = link_existing_user(
                name.strip(), email_field({"email": email}), role,
                management=Auth0Management(), subject=auth0_user_id,
            )
        except Problem as error:
            raise click.ClickException(error.message) from None
        click.echo(f"Portal access granted: {account['email']} ({account['role']}). Use the existing Auth0 password.")

    @app.cli.command("seed-demo")
    @click.option("--email-domain", default="example.test", show_default=True)
    @click.option("--student-username", default="demo-student", show_default=True)
    def seed_demo(email_domain, student_username):
        """Provision one test user per role and add linked demo records."""
        ensure_indexes()
        password = os.getenv("DEMO_PASSWORD") or click.prompt("Demo password", hide_input=True, confirmation_prompt=True)
        if len(password) < 12:
            raise click.ClickException("Choose a demo password with at least 12 characters that meets your Auth0 policy.")
        management = Auth0Management()
        accounts = {}
        try:
            for role in ("admin", "tutor", "parent", "student"):
                if role == "student":
                    email = None
                    username = username_field({"username": student_username})
                else:
                    email = email_field({"email": f"demo-{role}@{email_domain}"})
                    username = None
                accounts[role] = provision(
                    f"Demo {role.title()}", email, role, username=username,
                    management=management, password=password, reuse=True,
                )
            seed_records(accounts["tutor"], accounts["parent"], accounts["student"])
        except Problem as error:
            raise click.ClickException(error.message) from None
        for role, account in accounts.items():
            click.echo(f"{role.capitalize():8} {account.get('username') or account['email']}")
        click.echo("Existing demo passwords stay as previously set.")

    @app.cli.command("invoice-worker")
    @click.option("--once", is_flag=True, help="Process the current queue and exit.")
    def invoice_worker(once):
        """Extend bookings and deliver invoices and lesson reminder emails."""
        if not app.config["SMTP_HOST"] or not app.config["MAIL_FROM"]:
            app.logger.warning("Configure SMTP_HOST and MAIL_FROM to deliver invoice email.")
        while True:
            try:
                maintain_series()
                refresh_overdue()
                enqueue_reminders()
                while deliver_next():
                    pass
                while deliver_next_reminder():
                    pass
            except PyMongoError:
                if once:
                    raise
                app.logger.exception("Invoice worker will retry MongoDB in 5 seconds.")
            if once:
                return
            sleep(5)
