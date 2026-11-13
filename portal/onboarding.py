"""Parent welcome and child access emails, delivered by the background worker."""
from datetime import timedelta
from email.message import EmailMessage
from email.utils import format_datetime, parseaddr
import hashlib
import secrets
import smtplib
import ssl

from flask import current_app, render_template
from pymongo import ReturnDocument

from .billing import email_configuration_reason, email_error_reason
from .core import Problem, db, email_field, now

MAX_ATTEMPTS = 3


def enqueue_onboarding(parent_id, *, existing_account=False, student_id=None, transaction=None):
    """Queue once per parent or newly linked child, in the relationship transaction."""
    timestamp = now()
    key = f"student:{parent_id}:{student_id}" if student_id else f"welcome:{parent_id}"
    db().onboarding_deliveries.update_one({"_id": key}, {"$setOnInsert": {
        "parent_id": parent_id, "student_id": student_id, "existing_account": existing_account,
        "state": "queued", "attempts": 0, "next_at": timestamp, "created_at": timestamp,
    }}, upsert=True, session=transaction)
    db().onboarding_deliveries.update_one({"_id": key, "state": "skipped"}, {
        "$set": {"state": "queued", "attempts": 0, "next_at": timestamp},
        "$unset": {"reason": ""},
    }, session=transaction)


def onboarding_contents(job):
    # Import here so account provisioning can enqueue without an import cycle.
    from .accounts import Auth0Management

    parent = db().users.find_one({"_id": job["parent_id"], "role": "parent", "active": True})
    if not parent:
        return None
    email_field(parent)
    query = {"parent_id": parent["_id"], "role": "student", "active": True}
    if job.get("student_id"):
        query["_id"] = job["student_id"]
        # A welcome sent after linking may already contain this child's access details.
        if db().onboarding_deliveries.find_one({
            "_id": f"welcome:{parent['_id']}", "state": "sent", "student_ids": job["student_id"],
        }):
            return None
    students = list(db().users.find(query).sort("name", 1))
    if job.get("student_id") and not students:
        return None
    management = Auth0Management()
    setup_url = None
    if not job.get("student_id") and not job.get("existing_account"):
        setup_url = management.setup_link(parent["auth0_sub"])
    for student in students:
        if student.get("username_sync_pending"):
            raise Problem("Finish saving the student's pending username before sending their setup email.", 409)
        student["setup_url"] = management.setup_link(student["auth0_sub"])
    origin = current_app.config["APP_BASE_URL"]
    return {
        "parent": parent, "students": students, "setup_url": setup_url,
        "student_access_only": bool(job.get("student_id")),
        "links": {name: origin + path for name, path in {
            "login": "/login", "availability": "/availability", "lessons": "/lessons",
            "course_plans": "/course-plans", "homework": "/homework", "invoices": "/invoices",
        }.items()},
    }


def validate_recipients(contents):
    """Recheck ownership after Auth0 and SMTP network calls, before sending credentials."""
    parent = contents["parent"]
    if not db().users.find_one({
        "_id": parent["_id"], "role": "parent", "active": True,
        "email": parent["email"], "auth0_sub": parent["auth0_sub"],
    }, {"_id": 1}):
        raise Problem("The parent account changed while preparing the email. Delivery will recheck account access.", 409)
    for student in contents["students"]:
        if not db().users.find_one({
            "_id": student["_id"], "parent_id": parent["_id"], "role": "student", "active": True,
            "auth0_sub": student["auth0_sub"], "username": student.get("username"),
            "username_sync_pending": {"$ne": True},
        }, {"_id": 1}):
            raise Problem("A student account link changed while preparing the email. Delivery will recheck family access.", 409)


def send_onboarding(job, contents):
    config = current_app.config
    sender = config["MAIL_FROM"].strip()
    address = contents["parent"]["email"]
    message = EmailMessage()
    message["From"], message["To"], message["Reply-To"] = sender, address, sender
    message["Subject"] = "Your child's Lesson Desk access" if contents["student_access_only"] else "Welcome to Lesson Desk"
    message["Date"] = format_datetime(now())
    delivery_id = hashlib.sha256(job["_id"].encode()).hexdigest()
    message["Message-ID"] = f"<onboarding-{delivery_id}-{job.get('attempts', 0)}@{config['AUTH0_DOMAIN']}>"
    message.set_content(render_template("email/onboarding.txt", **contents))
    message.add_alternative(render_template("email/onboarding.html", **contents), subtype="html")
    context = ssl.create_default_context()
    connection = (smtplib.SMTP_SSL(config["SMTP_HOST"], config["SMTP_PORT"], context=context, timeout=20)
                  if config["SMTP_SECURITY"] == "ssl" else smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"], timeout=20))
    try:
        if config["SMTP_SECURITY"] == "starttls":
            connection.starttls(context=context)
        if config["SMTP_USERNAME"]:
            connection.login(config["SMTP_USERNAME"], config["SMTP_PASSWORD"])
        validate_recipients(contents)
        connection.send_message(message, from_addr=parseaddr(sender)[1], to_addrs=[address])
    finally:
        connection.close()


def deliver_next_onboarding():
    timestamp, lease = now(), secrets.token_hex(16)
    queue = db().onboarding_deliveries
    queue.update_many({"attempts": {"$gte": MAX_ATTEMPTS}, "$or": [
        {"state": "queued"}, {"state": "sending", "lease_until": {"$lte": timestamp}},
    ]}, {"$set": {"state": "failed", "reason": "Automatic onboarding delivery stopped after three attempts."},
         "$unset": {"lease": "", "lease_until": ""}})
    reason = email_configuration_reason()
    if reason:
        queue.update_many({"state": "queued"}, {"$set": {"reason": reason}})
        return False
    job = queue.find_one_and_update({"attempts": {"$lt": MAX_ATTEMPTS}, "$or": [
        {"state": "queued", "next_at": {"$lte": timestamp}},
        {"state": "sending", "lease_until": {"$lte": timestamp}},
    ]}, {"$set": {"state": "sending", "lease": lease, "lease_until": timestamp + timedelta(minutes=5)},
         "$inc": {"attempts": 1}}, sort=[("next_at", 1)], return_document=ReturnDocument.AFTER)
    if not job:
        return False
    unset = {"lease": "", "lease_until": ""}
    try:
        contents = onboarding_contents(job)
        if contents:
            send_onboarding(job, contents)
    except (Problem, OSError, smtplib.SMTPException, ValueError) as error:
        stopped = job["attempts"] >= MAX_ATTEMPTS
        reason = error.message if isinstance(error, Problem) else email_error_reason(error)
        update = {"state": "failed" if stopped else "queued", "reason": reason, "last_error_at": now()}
        if not stopped:
            update["next_at"] = now() + timedelta(seconds=30 * 2 ** (job["attempts"] - 1))
        current_app.logger.warning("Onboarding email delivery %s (%s).", "stopped" if stopped else "will retry", type(error).__name__)
    else:
        if contents:
            update = {"state": "sent", "sent_at": now(), "student_ids": [student["_id"] for student in contents["students"]]}
            unset["reason"] = ""
        else:
            update = {"state": "skipped", "reason": "Account links changed, or the welcome email already included this child."}
        unset["next_at"] = ""
    queue.update_one({"_id": job["_id"], "lease": lease}, {"$set": update, "$unset": unset})
    return True
