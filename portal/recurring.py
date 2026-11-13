"""Keep ongoing weekly bookings twelve weeks ahead, preserving local wall time."""

from datetime import timedelta

from bson import ObjectId

from .core import Problem, atomic, date_field, db, local_datetime, lock_accounts, now, time_minutes, today, zone
from .scheduling import verify_slot

HORIZON_WEEKS = 12


def verify_recurring_conflicts(series, transaction):
    """Check the whole ongoing reservation, including bookings far in the future."""
    first = date_field(series["first_on"])
    offset = first.weekday() * 1440 + time_minutes(series["time"])
    parties = [{"tutor_id": series["tutor_id"]}, {"student_id": series["student_id"]}]
    for other in db().lesson_series.find({"active": True, "$or": parties}, session=transaction):
        if other["_id"] == series.get("_id"):
            continue
        other_offset = date_field(other["first_on"]).weekday() * 1440 + time_minutes(other["time"])
        if any(offset < other_offset + shift + other["minutes"] and other_offset + shift < offset + series["minutes"]
               for shift in (-10080, 0, 10080)):
            raise Problem("An ongoing weekly booking already reserves this time for the tutor or student.", 409)
    first_start = local_datetime(f"{first}T{series['time']}")
    for lesson in db().lessons.find({
        "$or": parties, "status": "scheduled", "end_at": {"$gt": first_start},
    }, {"start_at": 1, "end_at": 1}, session=transaction):
        day = lesson["start_at"].astimezone(zone()).date() - timedelta(days=1)
        last = lesson["end_at"].astimezone(zone()).date()
        while day <= last:
            if day >= first and day.weekday() == first.weekday():
                try:
                    start = local_datetime(f"{day}T{series['time']}")
                except Problem:
                    day += timedelta(days=1)
                    continue
                if start < lesson["end_at"] and start + timedelta(minutes=series["minutes"]) > lesson["start_at"]:
                    raise Problem(f"{day}: an existing lesson conflicts with this weekly booking.", 409)
            day += timedelta(days=1)


def booking_parties(tutor_id, student_id, transaction):
    student = db().users.find_one({"_id": student_id}, {"parent_id": 1}, session=transaction)
    parent_id = student.get("parent_id") if student else None
    if not parent_id:
        raise Problem("An administrator must link a parent before scheduling lessons.", 409)
    lock_accounts(transaction, {tutor_id: "tutor", student_id: "student", parent_id: "parent"})
    return parent_id


def series_record(tutor_id, student_id, enrollment, title, first, minutes):
    return {
        "_id": ObjectId(), "tutor_id": tutor_id, "student_id": student_id,
        "topic_id": enrollment["_id"], "topic": enrollment.get("topic", "General lessons"),
        "title": title, "minutes": minutes, "first_on": first[:10], "time": first[11:],
        "next_on": first[:10], "active": True, "created_at": now(), "next_check_at": now(),
    }


def extend_in_transaction(series, until, transaction, *, strict=False):
    database = db()
    enrollment = database.enrollments.find_one({
        "_id": series["topic_id"], "tutor_id": series["tutor_id"], "student_id": series["student_id"],
    }, session=transaction)
    if not enrollment:
        raise Problem("The learning topic for this weekly booking is no longer linked.", 409)
    day = date_field(series["next_on"])
    created = 0
    issue = None
    while day < until:
        try:
            start = local_datetime(f"{day}T{series['time']}")
            end = start + timedelta(minutes=series["minutes"])
            if start > now() and not database.lessons.find_one({
                "series_id": series["_id"], "occurrence_on": day.isoformat(),
            }, {"_id": 1}, session=transaction):
                verify_slot(series["tutor_id"], series["student_id"], start, end,
                            excluding_series=series["_id"], session=transaction)
                database.lessons.insert_one({
                    "tutor_id": series["tutor_id"], "student_id": series["student_id"],
                    "topic_id": enrollment["_id"], "topic": enrollment.get("topic", "General lessons"),
                    "title": series["title"], "start_at": start, "end_at": end,
                    "status": "scheduled", "rate_cents": enrollment["rate_cents"],
                    "invoice_id": None, "created_at": now(),
                    "series_id": series["_id"], "occurrence_on": day.isoformat(),
                }, session=transaction)
                created += 1
        except Problem as error:
            if strict:
                raise Problem(f"{day}: {error.message}", error.status) from None
            issue = f"{day}: {error.message}"
            break
        day += timedelta(weeks=1)
    database.lesson_series.update_one({"_id": series["_id"], "active": True}, {"$set": {
        "next_on": day.isoformat(), "blocked_reason": issue,
        "next_check_at": now() + timedelta(minutes=5 if issue else 60),
    }}, session=transaction)
    return created


def maintain_series(*, until=None, scope=None):
    """Called by the worker and calendar reads; locks serialize with bookings."""
    until = until or today() + timedelta(weeks=HORIZON_WEEKS)
    query = {"active": True, **(scope or {})}
    if scope is None:
        query["next_check_at"] = {"$lte": now()}
    else:
        query["next_on"] = {"$lt": until.isoformat()}
    for record in db().lesson_series.find(query, {"_id": 1}):
        def extend(transaction):
            series = db().lesson_series.find_one({"_id": record["_id"], "active": True}, session=transaction)
            if not series:
                return
            booking_parties(series["tutor_id"], series["student_id"], transaction)
            extend_in_transaction(series, until, transaction)

        try:
            atomic(extend)
        except Problem as error:
            db().lesson_series.update_one({"_id": record["_id"], "active": True}, {"$set": {
                "blocked_reason": error.message, "next_check_at": now() + timedelta(minutes=5),
            }})
