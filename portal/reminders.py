from flask import url_for

from .billing import email_configuration_reason, valid_abn
from .core import Problem, db, email_field
from .scheduling import availability_owner


def billing_complete(profile):
    if not profile.get("name") or not valid_abn(profile.get("abn", "")):
        return False
    if not isinstance(profile.get("due_days"), int) or not 0 <= profile["due_days"] <= 365:
        return False
    try:
        email_field(profile)
    except Problem:
        return False
    return True


def setup_reminders(user):
    reminders = []
    role, user_id = user["role"], user["_id"]

    def add(text, endpoint=None, label="Complete setup", **values):
        reminders.append({"text": text, "url": url_for(endpoint, **values) if endpoint else None, "label": label})

    if role == "tutor":
        cancellations = list(db().lessons.find({
            "tutor_id": user_id, "cancellation_fee_pending": True,
        }, {"title": 1}).sort("cancelled_at", 1).limit(10))
        for lesson in cancellations:
            add(f"A parent cancelled {lesson['title']}. Review their reason and decide whether to charge a cancellation fee.",
                "lessons.detail", "Review cancellation", lesson_id=str(lesson["_id"]))
        if not db().weekly_availability.find_one({role + "_id": user_id}, {"_id": 1}):
            add("Your weekly availability is missing. Set your hours so lessons can be booked.", "lessons.availability", "Set availability")
    elif role == "parent":
        for child in db().users.find({"parent_id": user_id, "role": "student", "active": True}, {"name": 1}):
            if not db().weekly_availability.find_one(availability_owner(user_id, child["_id"]), {"_id": 1}):
                add(f"Weekly availability is missing for {child['name']}. Set their hours so lessons can be booked.",
                    "lessons.availability", "Set availability", student_id=str(child["_id"]))
    if role == "tutor":
        if not billing_complete(user.get("billing") or {}):
            add("Your billing profile is incomplete. Add your billing name, ABN, email and payment due period.", "billing.profile", "Complete billing profile")
        if not db().enrollments.find_one({"tutor_id": user_id}, {"_id": 1}):
            add("No students or learning topics are linked to your account. Ask your administrator to add them.")
        blocked = db().lesson_series.find_one({"tutor_id": user_id, "active": True, "blocked_reason": {"$type": "string"}})
        if blocked:
            add("An ongoing weekly booking needs attention. " + blocked["blocked_reason"], "lessons.index", "Review booking")
        if db().invoices.find_one({"tutor_id": user_id, "delivery.state": "failed"}, {"_id": 1}):
            add("Invoice email delivery has stopped after three attempts. An administrator must retry it.", "billing.index", "View delivery reason")
    elif role == "parent":
        if not db().users.find_one({"parent_id": user_id, "role": "student", "active": True}, {"_id": 1}):
            add("No students are linked to your account. Ask your administrator to link your family.")
    elif role == "student":
        if not user.get("parent_id"):
            add("Your parent link is missing. Ask your administrator to complete your account setup.")
        if not db().enrollments.find_one({"student_id": user_id}, {"_id": 1}):
            add("Your learning topics are missing. Ask your administrator to link your tutor and topics.")
    elif role == "admin":
        reason = email_configuration_reason()
        if reason:
            add(reason, "billing.index", "View email delivery")
        stopped = db().invoices.count_documents({"delivery.state": "failed"})
        if stopped:
            add(f"{stopped} invoice email{'s need' if stopped != 1 else ' needs'} an administrator to retry delivery.", "billing.index", "Review & retry")
        reminder_failures = db().lesson_reminder_deliveries.count_documents({"state": "failed"})
        if reminder_failures:
            add(f"{reminder_failures} lesson reminder email{'s have' if reminder_failures != 1 else ' has'} stopped after three attempts. Check the mail service and worker logs.")
        if db().users.find_one({"role": "student", "active": True, "parent_id": None}, {"_id": 1}):
            add("Student account setup is incomplete: a parent link is missing.", "accounts.students", "Complete student links")
        if db().users.find_one({"username_sync_pending": True}, {"_id": 1}):
            add("A student username is waiting to finish syncing with Auth0.", "accounts.index", "Complete account setup")
    return reminders
