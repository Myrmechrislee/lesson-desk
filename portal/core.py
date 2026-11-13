from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import wraps
import re

from bson import ObjectId
from flask import abort, current_app, g, redirect, url_for
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

ROLES = ("admin", "tutor", "parent", "student")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
STATUSES = {
    "scheduled": "Scheduled",
    "completed": "Completed",
    "did_not_attend": "Didn't attend",
    "cancelled_late": "Cancelled within 24 hours",
    "cancelled": "Cancelled",
}
FINAL_STATUSES = tuple(status for status in STATUSES if status != "scheduled")


class Problem(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status
        super().__init__(message)


def db():
    return current_app.extensions["db"]


def zone():
    return current_app.extensions["zone"]


def now():
    return datetime.now(UTC)


def today():
    return now().astimezone(zone()).date()


def requires(*roles):
    def decorator(view):
        @wraps(view)
        def protected(*args, **kwargs):
            if not g.user:
                return redirect(url_for("accounts.login"))
            if g.user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return protected
    return decorator


def object_id(value):
    if not ObjectId.is_valid(value):
        abort(404)
    return ObjectId(value)


def text_field(form, key, label, *, limit, required=False):
    value = form.get(key, "").strip()
    if (required and not value) or len(value) > limit or "\x00" in value:
        requirement = f"Enter {label} (up to {limit} characters)." if required else (
            f"Keep {label} to {limit} characters or fewer."
        )
        raise Problem(requirement)
    return value


def email_field(form, key="email"):
    email = text_field(form, key, "an email address", limit=254, required=True).lower()
    local, _, host = email.rpartition("@")
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if (
        not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}", local)
        or local.startswith(".") or local.endswith(".") or ".." in local
        or not re.fullmatch(rf"{label}(?:\.{label})+", host)
    ):
        raise Problem("Enter a valid email address.")
    return email


def int_field(form, key, label, minimum, maximum):
    try:
        value = int(form.get(key, ""))
    except ValueError:
        value = minimum - 1
    if not minimum <= value <= maximum:
        raise Problem(f"Enter {label} from {minimum} to {maximum}.")
    return value


def cents_field(form, key):
    value = form.get(key, "").strip()
    if not re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,2})?", value):
        raise Problem("Enter a lesson rate in AUD with up to two decimal places.")
    return int(Decimal(value) * 100)


def date_field(value):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise Problem("Choose a valid date.") from None
    if not 1900 <= parsed.year <= 2199:
        raise Problem("Choose a date between 1900 and 2199.")
    return parsed


def time_minutes(value, *, end=False):
    if not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
        raise Problem("Enter a valid time.")
    try:
        clock = time.fromisoformat(value)
    except ValueError:
        raise Problem("Enter a valid time.") from None
    if end and clock == time.min:
        return 1440
    return clock.hour * 60 + clock.minute


def boundary(day, minute=0):
    wall_time = datetime.combine(day, time.min) + timedelta(minutes=minute)
    return wall_time.replace(tzinfo=zone()).astimezone(UTC)


def local_datetime(value):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}", value):
        raise Problem("Choose a date and time.")
    date_field(value[:10])
    try:
        wall = datetime.fromisoformat(value)
    except ValueError:
        raise Problem("Choose a valid date and time.") from None
    local = wall.replace(tzinfo=zone())
    instant = local.astimezone(UTC)
    if instant.astimezone(zone()).replace(tzinfo=None) != wall:
        raise Problem("Choose a time that exists after the daylight saving change.")
    if local.utcoffset() != local.replace(fold=1).utcoffset():
        raise Problem("Choose a time outside the repeated daylight saving hour.")
    return instant


def stamp(value, style="full"):
    if not value:
        return ""
    local = value.astimezone(zone())
    formats = {
        "full": "%a, %d %b %Y · %H:%M",
        "day": "%a, %d %b",
        "time": "%H:%M",
        "date": "%d %b %Y",
        "input": "%Y-%m-%dT%H:%M",
    }
    return local.strftime(formats[style])


def day_label(value):
    return date.fromisoformat(value).strftime("%d %b %Y")


def money(cents):
    return f"${Decimal(cents) / 100:,.2f}"


def people(ids):
    return {
        user["_id"]: user
        for user in db().users.find({"_id": {"$in": list(set(ids))}}, {"name": 1, "email": 1, "username": 1})
    }


def atomic(callback):
    with db().client.start_session() as session:
        return session.with_transaction(
            callback, read_concern=ReadConcern("snapshot"), write_concern=WriteConcern("majority")
        )


def lock_accounts(session, expected_roles):
    # Every schedule or relationship mutation writes the participating account rows.
    # Concurrent transactions then retry against a fresh snapshot before checking overlaps.
    for user_id, role in sorted(expected_roles.items()):
        result = db().users.update_one(
            {"_id": user_id, "active": True, "role": role},
            {"$inc": {"revision": 1}}, session=session,
        )
        if not result.matched_count:
            raise Problem("An account linked to this operation has changed. Reload the page.", 409)


def ensure_indexes():
    database = db()
    database.sessions.create_index("id", unique=True)
    # Username-only students have no email. Replace the original index, which
    # treats a missing email as a unique null, without dropping email uniqueness.
    database.users.create_index(
        "email", name="email_present_unique", unique=True,
        partialFilterExpression={"email": {"$type": "string"}},
    )
    if "email_1" in database.users.index_information():
        database.users.drop_index("email_1")
    database.users.create_index(
        "username", unique=True, partialFilterExpression={"username": {"$type": "string"}},
    )
    database.users.create_index("auth0_sub", unique=True, sparse=True)
    database.users.create_index([("parent_id", 1), ("role", 1)])
    legacy_links = list(database.enrollments.find(
        {"topic_key": {"$exists": False}}, {"tutor_id": 1, "student_id": 1},
    ))
    database.enrollments.update_many(
        {"topic_key": {"$exists": False}},
        {"$set": {"topic": "General lessons", "topic_key": "general lessons"}},
    )
    for link in legacy_links:
        database.lessons.update_many({
            "tutor_id": link["tutor_id"], "student_id": link["student_id"], "topic_id": {"$exists": False},
        }, {"$set": {"topic_id": link["_id"], "topic": "General lessons"}})
    database.enrollments.create_index([("tutor_id", 1), ("student_id", 1), ("topic_key", 1)], unique=True)
    if "tutor_id_1_student_id_1" in database.enrollments.index_information():
        database.enrollments.drop_index("tutor_id_1_student_id_1")
    database.enrollments.create_index("student_id")
    database.lessons.create_index([("tutor_id", 1), ("status", 1), ("start_at", 1)])
    database.lessons.create_index([("student_id", 1), ("status", 1), ("start_at", 1)])
    database.lessons.create_index([("tutor_id", 1), ("invoice_id", 1), ("student_id", 1)])
    database.lessons.create_index(
        [("series_id", 1), ("occurrence_on", 1)], unique=True,
        partialFilterExpression={"series_id": {"$type": "objectId"}, "occurrence_on": {"$type": "string"}},
    )
    database.lesson_series.create_index([("active", 1), ("next_check_at", 1)])
    database.lesson_series.create_index([("tutor_id", 1), ("student_id", 1)])
    database.weekly_availability.create_index("tutor_id")
    database.weekly_availability.create_index("parent_id")
    database.weekly_availability.create_index([("parent_id", 1), ("student_id", 1)])
    database.availability_exceptions.create_index([("tutor_id", 1), ("start_at", 1)])
    database.availability_exceptions.create_index([("parent_id", 1), ("start_at", 1)])
    database.availability_exceptions.create_index([("parent_id", 1), ("student_id", 1), ("start_at", 1)])
    database.change_requests.create_index(
        "lesson_id", unique=True, partialFilterExpression={"status": "pending"}
    )
    database.change_requests.create_index([("tutor_id", 1), ("created_at", -1)])
    database.change_requests.create_index([("parent_id", 1), ("created_at", -1)])
    database.change_requests.create_index([("student_id", 1), ("status", 1)])
    database.homework.create_index([("tutor_id", 1), ("created_at", -1)])
    database.homework.create_index([("student_id", 1), ("created_at", -1)])
    database.homework_photos.create_index([("homework_id", 1), ("created_at", 1)])
    database.announcements.create_index([("created_at", -1), ("_id", -1)])
    database.announcement_files.create_index([("announcement_id", 1), ("created_at", 1)])
    database.announcement_reads.create_index([("parent_id", 1), ("announcement_id", 1)], unique=True)
    database.announcement_views.create_index([("user_id", 1), ("announcement_id", 1)], unique=True)
    database.lesson_reminder_deliveries.create_index([("state", 1), ("next_at", 1)])
    database.whiteboard_assets.create_index([("lesson_id", 1), ("created_at", 1)])
    database.whiteboard_live.create_index("expires_at", expireAfterSeconds=0)
    database.whiteboard_live.create_index([("lesson_id", 1), ("seen_at", 1)])
    database.comments.create_index([("kind", 1), ("target_id", 1), ("created_at", 1)])
    database.comments.create_index([("kind", 1), ("target_id", 1), ("family_id", 1)])
    database.comment_images.create_index("comment_id")
    database.notifications.create_index([("user_id", 1), ("created_at", -1)])
    database.notifications.create_index([("user_id", 1), ("read_at", 1)])
    database.lessons.create_index([("tutor_id", 1), ("cancellation_fee_pending", 1)])
    database.course_plans.create_index([("tutor_id", 1), ("student_id", 1)])
    database.course_plan_files.create_index([("topic_id", 1), ("created_at", 1)])
    database.ai_requests.create_index("expires_at", expireAfterSeconds=0)
    database.call_participants.create_index("expires_at", expireAfterSeconds=0)
    database.call_participants.create_index([("lesson_id", 1), ("seen_at", 1)])
    database.call_signals.create_index("expires_at", expireAfterSeconds=0)
    database.call_signals.create_index([("lesson_id", 1), ("target_id", 1), ("_id", 1)])
    database.invoices.create_index("number", unique=True)
    database.invoices.create_index([("tutor_id", 1), ("issued_at", -1)])
    database.invoices.create_index([("parent_id", 1), ("issued_at", -1)])
    database.invoices.create_index([("status", 1), ("due_at", 1)])
    database.invoices.create_index([("delivery.state", 1), ("delivery.next_at", 1)])
