"""Durable lesson reminders delivered by the existing background worker."""
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
import hashlib
import secrets
import smtplib
import ssl

from flask import current_app
from pymongo import ReturnDocument

from .billing import email_configuration_reason, email_error_reason
from .calendar_feed import account_scope
from .core import Problem, boundary, db, email_field, now, people, stamp, zone

MAX_ATTEMPTS = 3


def weekly_window(timestamp):
    """Sunday at 18:00 local time prepares the following Monday–Sunday."""
    local = timestamp.astimezone(zone())
    if local.weekday() != 6 or local.hour < 18:
        return None
    monday = local.date() + timedelta(days=1)
    return monday, monday + timedelta(days=7)


def recipients(lessons):
    ids = {row["tutor_id"] for row in lessons} | {row["student_id"] for row in lessons}
    students = list(db().users.find({"_id": {"$in": list(ids)}, "role": "student", "active": True}, {"parent_id": 1}))
    ids |= {student["parent_id"] for student in students if student.get("parent_id")}
    accounts = []
    for user in db().users.find({"_id": {"$in": list(ids)}, "active": True, "role": {"$in": ["tutor", "parent", "student"]}}):
        try:
            email_field(user)
        except Problem:
            continue
        accounts.append(user)
    return accounts


def enqueue_reminders(timestamp=None):
    timestamp = timestamp or now()
    due = list(db().lessons.find({"status": "scheduled", "start_at": {"$gt": timestamp, "$lte": timestamp + timedelta(hours=24)}}))
    queue = db().lesson_reminder_deliveries
    for user in recipients(due):
        allowed = {row["_id"] for row in db().lessons.find({**account_scope(user), "_id": {"$in": [lesson["_id"] for lesson in due]}}, {"_id": 1})}
        for lesson in due:
            if lesson["_id"] not in allowed:
                continue
            version = hashlib.sha256((lesson["start_at"].isoformat() + lesson["end_at"].isoformat()).encode()).hexdigest()[:20]
            queue.update_one({"_id": f"lesson:{lesson['_id']}:{user['_id']}:{version}"}, {"$setOnInsert": {
                "kind": "lesson", "user_id": user["_id"], "lesson_id": lesson["_id"], "start_at": lesson["start_at"],
                "end_at": lesson["end_at"], "state": "queued", "attempts": 0, "next_at": timestamp, "created_at": timestamp,
            }}, upsert=True)
    window = weekly_window(timestamp)
    if window:
        start, end = window
        upcoming = list(db().lessons.find({"status": "scheduled", "start_at": {"$gte": boundary(start), "$lt": boundary(end)}}))
        for user in recipients(upcoming):
            queue.update_one({"_id": f"week:{start}:{user['_id']}"}, {"$setOnInsert": {
                "kind": "week", "user_id": user["_id"], "week_start": start.isoformat(), "week_end": end.isoformat(),
                "state": "queued", "attempts": 0, "next_at": timestamp, "created_at": timestamp,
            }}, upsert=True)


def current_message(job, timestamp):
    user = db().users.find_one({"_id": job["user_id"], "active": True, "role": {"$in": ["tutor", "parent", "student"]}})
    if not user:
        return None
    try:
        address = email_field(user)
    except Problem:
        return None
    scope = {**account_scope(user), "status": "scheduled"}
    if job["kind"] == "lesson":
        scope.update({"_id": job["lesson_id"], "start_at": job["start_at"], "end_at": job["end_at"]})
        if job["start_at"] <= timestamp:
            return None
    else:
        start, end = (datetime.fromisoformat(job[key]).date() for key in ("week_start", "week_end"))
        if timestamp >= boundary(end):
            return None
        scope["start_at"] = {"$gte": max(boundary(start), timestamp), "$lt": boundary(end)}
    lessons = list(db().lessons.find(scope).sort("start_at", 1))
    if not lessons:
        return None
    names = people(key for lesson in lessons for key in (lesson["student_id"], lesson["tutor_id"]))
    subject = "Lesson reminder: " + stamp(lessons[0]["start_at"]) if job["kind"] == "lesson" else "Your lessons for the coming week"
    lines = [f"Hello {user['name']},", "", "Your lesson starts within 24 hours." if job["kind"] == "lesson" else "Here is your lesson schedule for the coming week.", ""]
    for lesson in lessons:
        student = names.get(lesson["student_id"], {}).get("name", "Student")
        tutor = names.get(lesson["tutor_id"], {}).get("name", "Tutor")
        lines += [f"{lesson.get('topic') or lesson.get('title') or 'Lesson'} — {student}",
                  f"{stamp(lesson['start_at'])}–{stamp(lesson['end_at'], 'time')} ({current_app.config['APP_TIMEZONE']})",
                  f"Tutor: {tutor}", current_app.config["APP_BASE_URL"] + f"/lessons/{lesson['_id']}", ""]
    lines += ["Check Lesson Desk for the latest schedule and any changes."]
    return address, subject, "\n".join(lines)


def send_reminder(job, contents):
    address, subject, body = contents
    config = current_app.config
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = config["MAIL_FROM"], address, subject
    message["Date"] = format_datetime(now())
    message["Message-ID"] = f"<reminder-{hashlib.sha256(job['_id'].encode()).hexdigest()}@{config['AUTH0_DOMAIN']}>"
    message.set_content(body)
    context = ssl.create_default_context()
    connection = (smtplib.SMTP_SSL(config["SMTP_HOST"], config["SMTP_PORT"], context=context, timeout=20)
                  if config["SMTP_SECURITY"] == "ssl" else smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"], timeout=20))
    try:
        if config["SMTP_SECURITY"] == "starttls":
            connection.starttls(context=context)
        if config["SMTP_USERNAME"]:
            connection.login(config["SMTP_USERNAME"], config["SMTP_PASSWORD"])
        connection.send_message(message, to_addrs=[address])
    finally:
        connection.close()


def deliver_next_reminder():
    timestamp, lease = now(), secrets.token_hex(16)
    queue = db().lesson_reminder_deliveries
    queue.update_many({"attempts": {"$gte": MAX_ATTEMPTS}, "$or": [{"state": "queued"}, {"state": "sending", "lease_until": {"$lte": timestamp}}]},
                      {"$set": {"state": "failed", "reason": "Automatic reminder delivery stopped after three attempts."}})
    reason = email_configuration_reason()
    if reason:
        queue.update_many({"state": "queued"}, {"$set": {"reason": reason}})
        return False
    job = queue.find_one_and_update({"attempts": {"$lt": MAX_ATTEMPTS}, "$or": [
        {"state": "queued", "next_at": {"$lte": timestamp}}, {"state": "sending", "lease_until": {"$lte": timestamp}},
    ]}, {"$set": {"state": "sending", "lease": lease, "lease_until": timestamp + timedelta(minutes=5)}, "$inc": {"attempts": 1}},
        sort=[("next_at", 1)], return_document=ReturnDocument.AFTER)
    if not job:
        return False
    unset = {"lease": "", "lease_until": ""}
    contents = current_message(job, timestamp)
    if not contents:
        update = {"state": "skipped", "reason": "The lesson or account changed, or this reminder expired."}
    else:
        try:
            send_reminder(job, contents)
        except (OSError, smtplib.SMTPException, ValueError) as error:
            stopped = job["attempts"] >= MAX_ATTEMPTS
            update = {"state": "failed" if stopped else "queued", "reason": email_error_reason(error), "last_error_at": now()}
            if not stopped:
                update["next_at"] = now() + timedelta(seconds=30 * 2 ** (job["attempts"] - 1))
            current_app.logger.warning("Lesson reminder delivery %s (%s).", "stopped" if stopped else "will retry", type(error).__name__)
        else:
            update = {"state": "sent", "sent_at": now()}
            unset.update({"reason": "", "next_at": ""})
    queue.update_one({"_id": job["_id"], "lease": lease}, {"$set": update, "$unset": unset})
    return True
