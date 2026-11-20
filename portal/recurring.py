"""Keep ongoing weekly bookings twelve weeks ahead, preserving local wall time."""

from datetime import timedelta

from bson import ObjectId

from .core import Problem, atomic, boundary, date_field, db, local_datetime, lock_accounts, now, schedule_zone, time_minutes, today, zone
from .scheduling import verify_slot, weekly_intervals

HORIZON_WEEKS = 12


def verify_recurring_conflicts(series, transaction):
    """Check the whole ongoing reservation, including bookings far in the future."""
    first = date_field(series["first_on"])
    tz = schedule_zone(series)
    parties = [{"tutor_id": series["tutor_id"]}, {"student_id": series["student_id"]}]
    for other in db().lesson_series.find({"active": True, "$or": parties}, session=transaction):
        if other["_id"] == series.get("_id"):
            continue
        # Compare UTC occurrences over both seasonal cycles: schedules in
        # different zones may conflict in just one season.
        anchor = max(first, date_field(other["first_on"]))
        left = boundary(anchor, tz=tz)
        right = boundary(anchor + timedelta(weeks=106), tz=tz)

        def rule(record):
            minute = time_minutes(record["time"])
            return {"weekday": date_field(record["first_on"]).weekday(),
                    "start_min": minute, "end_min": minute + record["minutes"],
                    "timezone": schedule_zone(record).key}

        candidates = list(weekly_intervals(rule(other), left, right))
        if any(start < other_end and end > other_start
               for start, end in weekly_intervals(rule(series), left, right)
               for other_start, other_end in candidates):
            raise Problem("An ongoing weekly booking already reserves this time for the tutor or student.", 409)
    first_start = local_datetime(f"{first}T{series['time']}", tz=tz)
    for lesson in db().lessons.find({
        "$or": parties, "status": "scheduled", "end_at": {"$gt": first_start},
    }, {"start_at": 1, "end_at": 1}, session=transaction):
        day = lesson["start_at"].astimezone(tz).date() - timedelta(days=1)
        last = lesson["end_at"].astimezone(tz).date()
        while day <= last:
            if day >= first and day.weekday() == first.weekday():
                try:
                    start = local_datetime(f"{day}T{series['time']}", tz=tz)
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


def series_record(tutor_id, student_id, enrollment, title, first, minutes, *, skip_availability=False, created_by=None):
    return {
        "_id": ObjectId(), "tutor_id": tutor_id, "student_id": student_id,
        "topic_id": enrollment["_id"], "topic": enrollment.get("topic", "General lessons"),
        "title": title, "minutes": minutes, "first_on": first[:10], "time": first[11:],
        "start_at": local_datetime(first), "timezone": zone().key,
        "skip_availability": skip_availability, "created_by": created_by or tutor_id,
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
            start = local_datetime(f"{day}T{series['time']}", tz=schedule_zone(series))
            end = start + timedelta(minutes=series["minutes"])
            if start > now() and not database.lessons.find_one({
                "series_id": series["_id"], "occurrence_on": day.isoformat(),
            }, {"_id": 1}, session=transaction):
                verify_slot(series["tutor_id"], series["student_id"], start, end,
                            excluding_series=series["_id"],
                            skip_availability=series.get("skip_availability", False), session=transaction)
                database.lessons.insert_one({
                    "tutor_id": series["tutor_id"], "student_id": series["student_id"],
                    "topic_id": enrollment["_id"], "topic": enrollment.get("topic", "General lessons"),
                    "title": series["title"], "start_at": start, "end_at": end,
                    "status": "scheduled", "rate_cents": enrollment["rate_cents"],
                    "invoice_id": None, "created_at": now(),
                    "series_id": series["_id"], "occurrence_on": day.isoformat(),
                    "created_by": series.get("created_by", series["tutor_id"]),
                    "availability_override": series.get("skip_availability", False),
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
