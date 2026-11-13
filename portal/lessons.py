from datetime import timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .core import (
    FINAL_STATUSES, STATUSES, WEEKDAYS, Problem, atomic, boundary, date_field, db,
    int_field, local_datetime, lock_accounts, now, object_id, people, requires,
    stamp, text_field, time_minutes, today,
)
from .recurring import HORIZON_WEEKS, booking_parties, extend_in_transaction, maintain_series, series_record, verify_recurring_conflicts
from .scheduling import availability_calendar, availability_owner, change_calendar, occupied, open_slots, overlaps, verify_change_slot, verify_slot, weekly_windows

bp = Blueprint("lessons", __name__)
PARENT_LESSON_FIELDS = {
    "feedback": 0, "student_feedback": 0, "cancellation_reason": 0, "charge": 0, "rate_cents": 0,
}
STUDENT_LESSON_FIELDS = {
    "parent_feedback": 0, "cancellation_reason": 0, "charge": 0, "rate_cents": 0,
}
LATE_NOTICE = (
    "This request is within 24 hours of the lesson. Under your contract, you may still "
    "need to pay the full price for the original lesson. Your tutor will decide whether to charge it."
)
CONSISTENCY_NOTICE = (
    "We value consistency and believe it is important to help the student grow. "
    "We recommend to try rescheduling before cancelling."
)
PARENT_LATE_NOTICE = (
    "As per our contract, cancellation within 24 hours, is considered a late cancellation. "
    "You may still be required to pay for the lesson in full. "
    "The reason behind the cancellation will be taken into consideration."
)


def children():
    return list(db().users.find(
        {"role": "student", "active": True, "parent_id": g.user["_id"]}, {"name": 1},
    ).sort("name", 1))


def lesson_scope(role, user_id, student_ids=None):
    if role == "parent":
        return {"student_id": {"$in": student_ids}}
    return {f"{role}_id": user_id}


def lesson_fields(role):
    return {"parent": PARENT_LESSON_FIELDS, "student": STUDENT_LESSON_FIELDS}.get(role)


def cancellation_time(lesson, timestamp):
    if lesson["status"] in {"cancelled", "cancelled_late"}:
        return lesson.get("cancelled_at", lesson.get("finalized_at", timestamp))
    return timestamp


def can_charge_cancellation(lesson, timestamp):
    notice = lesson["start_at"] - cancellation_time(lesson, timestamp)
    return timedelta(0) <= notice < timedelta(hours=24)


def lesson_feedback(form):
    return (
        text_field(form, "parent_feedback", "feedback for the parent", limit=4000, required=True),
        text_field(form, "student_feedback", "feedback for the student", limit=4000),
    )


def get_lesson(lesson_id):
    role = g.user["role"]
    student_ids = [child["_id"] for child in children()] if role == "parent" else None
    query = {"_id": object_id(lesson_id), **lesson_scope(role, g.user["_id"], student_ids)}
    lesson = db().lessons.find_one(query, lesson_fields(role))
    if not lesson:
        abort(404)
    return lesson


def enrollment_for(tutor_id, student_id, session, *, topic_id=None, require_topic=False):
    query = {"tutor_id": tutor_id, "student_id": student_id}
    if topic_id:
        query["_id"] = topic_id
    rows = list(db().enrollments.find(query, session=session).limit(2))
    enrollment = rows[0] if rows else None
    if not enrollment:
        raise Problem("Choose a learning topic linked to this student and tutor.")
    if require_topic and not topic_id and len(rows) > 1:
        raise Problem("Choose the learning topic for this lesson.")
    return enrollment


def tutor_students():
    enrollments, names = tutor_topics()
    unique = {row["student_id"]: row for row in enrollments}
    return list(unique.values()), names


def tutor_topics():
    enrollments = list(db().enrollments.find({"tutor_id": g.user["_id"]}))
    names = people(row["student_id"] for row in enrollments)
    return sorted(enrollments, key=lambda row: (
        names[row["student_id"]]["name"].casefold(), row.get("topic", "General lessons").casefold(),
    )), names


def change_requests(role, account_id, student_ids=None):
    if role == "student":
        return [], []
    query = {f"{role}_id": account_id}
    if role == "parent":
        query["student_id"] = {"$in": student_ids}
    requests = db().change_requests
    pending = list(requests.find({**query, "status": "pending", "expires_at": {"$gt": now()}}).sort("original_start", 1))
    closed = list(requests.find({**query, "status": {"$ne": "pending"}}).sort("resolved_at", -1).limit(6))
    return pending, closed


@bp.get("/lessons")
@requires("tutor", "parent", "student")
def index():
    role = g.user["role"]
    child_profiles = children() if role == "parent" else []
    child_ids = [child["_id"] for child in child_profiles]
    selected = request.args.get("child", "")
    if selected:
        selected_id = object_id(selected)
        if selected_id not in child_ids:
            abort(404)
        child_ids = [selected_id]

    query = lesson_scope(role, g.user["_id"], child_ids)
    view = request.args.get("view", "upcoming") if role == "parent" else "week"
    if role == "parent":
        if view not in {"upcoming", "past", "all"}:
            raise Problem("Choose upcoming, past, or all lessons.")
        if view == "upcoming":
            query.update({"status": "scheduled", "start_at": {"$gt": now()}})
        elif view == "past":
            query["$or"] = [{"start_at": {"$lte": now()}}, {"status": {"$ne": "scheduled"}}]
    local_day = date_field(request.args["week"]) if "week" in request.args else today()
    week = local_day - timedelta(days=local_day.weekday())
    next_week = week + timedelta(days=7)
    maintain_series(until=max(next_week, today() + timedelta(weeks=HORIZON_WEEKS)),
                    scope=lesson_scope(role, g.user["_id"], child_ids))
    if role != "parent":
        query["start_at"] = {"$gte": boundary(week), "$lt": boundary(next_week)}

    rows = list(db().lessons.find(
        query, lesson_fields(role),
    ).sort("start_at", -1 if view == "past" else 1))
    changes, closed_changes = change_requests(role, g.user["_id"], child_ids)
    names = people(
        user_id for row in rows + changes + closed_changes for user_id in (row["student_id"], row["tutor_id"])
    )
    plan = []
    if role == "tutor":
        plan, student_names = tutor_topics()
        names.update(student_names)
        counts = {}
        for row in rows:
            if row["status"] not in {"cancelled", "cancelled_late"}:
                key = row.get("topic_id") or row["student_id"]
                counts[key] = counts.get(key, 0) + 1
        for row in plan:
            row["count"] = counts.get(row["_id"], 0) + (
                counts.get(row["student_id"], 0) if row.get("topic", "General lessons") == "General lessons" else 0
            )
    learning_topics = []
    if role in {"parent", "student"}:
        topic_scope = {"student_id": {"$in": child_ids}} if role == "parent" else {"student_id": g.user["_id"]}
        learning_topics = list(db().enrollments.find(topic_scope, {"student_id": 1, "tutor_id": 1, "topic": 1}))
        names.update(people(user_id for topic in learning_topics for user_id in (topic["student_id"], topic["tutor_id"])))

    return render_template(
        "lessons.html", lessons=rows, names=names, week=week, next_week=next_week,
        last_week=week - timedelta(days=7), end_of_week=next_week - timedelta(days=1),
        changes=changes, closed_changes=closed_changes, statuses=STATUSES, plan=plan,
        children=child_profiles, selected=selected, view=view, current=now(),
        learning_topics=learning_topics,
        series=list(db().lesson_series.find({"tutor_id": g.user["_id"], "active": True})) if role == "tutor" else [],
        announcements=list(db().announcements.find().sort([("created_at", -1), ("_id", -1)]).limit(3)) if role in {"tutor", "parent"} else [],
    )


@bp.get("/lessons/new")
@requires("tutor")
def new():
    enrollments, names = tutor_students()
    topics, _ = tutor_topics()
    return render_template("lesson_new.html", enrollments=enrollments, topics=topics, names=names,
                           default_day=today() + timedelta(days=1))


def repeat_options(form):
    mode = form.get("repeat") or ("count" if form.get("weeks") else "forever")
    if mode not in {"once", "count", "forever"}:
        raise Problem("Choose a single lesson, a fixed number of weeks, or weekly until stopped.")
    weeks = int_field(form, "weeks", "the number of weeks", 1, 52) if mode == "count" else (
        HORIZON_WEEKS if mode == "forever" else 1
    )
    return mode, weeks


def weekly_starts(first, weeks):
    local_datetime(first)
    day = date_field(first[:10])
    return [local_datetime(f"{day + timedelta(weeks=offset)}{first[10:]}") for offset in range(weeks)]


@bp.get("/lessons/booking-availability")
@requires("tutor")
def booking_availability():
    student_id = object_id(request.args.get("student_id"))
    enrollment_for(g.user["_id"], student_id, None)
    day = date_field(request.args.get("day"))
    minutes = int_field(request.args, "minutes", "a duration in minutes", 1, 720)
    student = db().users.find_one({"_id": student_id}, {"parent_id": 1})
    parent_id = student.get("parent_id") if student else None
    start, end = boundary(day), boundary(day + timedelta(days=1))
    bookings = list(occupied(g.user["_id"], student_id, start, end))

    def windows_for(owner_id, field):
        if not owner_id:
            return {"available": [], "unavailable": []}
        available, unavailable = weekly_windows(owner_id, start, end, owner_field=field,
                                                student_id=student_id if field == "parent_id" else None)
        busy = [(row["start_at"], row["end_at"]) for row in bookings
                if row["tutor_id"] == owner_id] if field == "tutor_id" else [
                    (row["start_at"], row["end_at"]) for row in bookings if row["student_id"] == student_id
                ]
        unavailable = sorted(unavailable + busy)
        free = []
        for left, right in available:
            cursor = max(left, start)
            right = min(right, end)
            for busy_left, busy_right in unavailable:
                if busy_right <= cursor or busy_left >= right:
                    continue
                if busy_left > cursor:
                    free.append((cursor, busy_left))
                cursor = max(cursor, busy_right)
                if cursor >= right:
                    break
            if cursor < right:
                free.append((cursor, right))
        def labels(windows):
            return list(dict.fromkeys(
                f"{stamp(max(left, start), 'time')}–{'24:00' if right >= end else stamp(right, 'time')}"
                for left, right in windows if left < end and right > start
            ))
        return {"available": labels(free), "unavailable": labels(unavailable)}

    synthetic = {"tutor_id": g.user["_id"], "student_id": student_id,
                 "start_at": start - timedelta(days=1),
                 "end_at": start - timedelta(days=1) + timedelta(minutes=minutes)}
    slots = open_slots(synthetic, day, parent_id=parent_id) if parent_id else []
    valid, reason = False, "Choose a start time within both calendars."
    if not parent_id:
        reason = "An administrator must link a parent before scheduling lessons."
    elif request.args.get("starts_at"):
        mode, weeks = repeat_options(request.args)
        candidate = None
        try:
            for candidate in weekly_starts(request.args["starts_at"], weeks):
                verify_slot(g.user["_id"], student_id, candidate, candidate + timedelta(minutes=minutes), session=None)
            if mode == "forever":
                first = request.args["starts_at"]
                verify_recurring_conflicts({"tutor_id": g.user["_id"], "student_id": student_id,
                    "first_on": first[:10], "time": first[11:], "minutes": minutes}, None)
            valid, reason = True, "This time fits both calendars and has no booking conflicts."
        except Problem as error:
            reason = f"{stamp(candidate)}: {error.message}" if candidate else error.message
    student_hours = windows_for(parent_id, "parent_id")
    return {"tutor": windows_for(g.user["_id"], "tutor_id"),
            "parent": student_hours, "student": student_hours, "slots": slots,
            "valid": valid, "reason": reason}


@bp.post("/lessons/new")
@requires("tutor")
def create():
    tutor_id = g.user["_id"]
    student_id = object_id(request.form.get("student_id"))
    first = request.form.get("starts_at", "")
    if local_datetime(first) <= now():
        raise Problem("Choose a lesson time in the future.")
    day = date_field(first[:10])
    minutes = int_field(request.form, "minutes", "a duration in minutes", 1, 720)
    duration = timedelta(minutes=minutes)
    mode, weeks = repeat_options(request.form)
    topic_id = object_id(request.form["topic_id"]) if request.form.get("topic_id") else None
    title = text_field(request.form, "title", "a lesson title", limit=100, required=True)
    starts = weekly_starts(first, weeks)
    if tutor_id == student_id:
        raise Problem("Choose a student account.")

    def schedule(transaction):
        booking_parties(tutor_id, student_id, transaction)
        enrollment = enrollment_for(tutor_id, student_id, transaction, topic_id=topic_id, require_topic=True)
        if mode == "forever":
            series = series_record(tutor_id, student_id, enrollment, title, first, minutes)
            verify_recurring_conflicts(series, transaction)
            db().lesson_series.insert_one(series, session=transaction)
            extend_in_transaction(series, day + timedelta(weeks=HORIZON_WEEKS), transaction, strict=True)
            return
        created_at = now()
        records = []
        for start in starts:
            end = start + duration
            try:
                verify_slot(tutor_id, student_id, start, end, session=transaction)
            except Problem as error:
                raise Problem(f"{stamp(start, 'full')}: {error.message}", error.status) from None
            records.append({
                "tutor_id": tutor_id, "student_id": student_id, "title": title,
                "start_at": start, "end_at": end, "status": "scheduled",
                "rate_cents": enrollment["rate_cents"], "invoice_id": None, "created_at": created_at,
                "topic_id": enrollment["_id"], "topic": enrollment.get("topic", "General lessons"),
            })
        db().lessons.insert_many(records, session=transaction)

    atomic(schedule)
    flash("Weekly lesson booked until you stop it. Future lessons are added automatically." if mode == "forever" else (
        "Lesson scheduled." if weeks == 1 else f"{weeks} weekly lessons scheduled."
    ))
    return redirect(url_for("lessons.index", week=day.isoformat()), code=303)


@bp.post("/lessons/series/<series_id>/stop")
@requires("tutor")
def stop_series(series_id):
    series_id = object_id(series_id)

    def stop(transaction):
        database = db()
        series = database.lesson_series.find_one({"_id": series_id, "tutor_id": g.user["_id"]}, session=transaction)
        if not series:
            abort(404)
        lock_accounts(transaction, {series["tutor_id"]: "tutor", series["student_id"]: "student"})
        future = {"series_id": series_id, "status": "scheduled", "start_at": {"$gt": now()}, "invoice_id": None}
        lesson_ids = [row["_id"] for row in database.lessons.find(future, {"_id": 1}, session=transaction)]
        database.change_requests.update_many(
            {"lesson_id": {"$in": lesson_ids}, "status": "pending"},
            {"$set": {"status": "withdrawn", "resolved_at": now()}}, session=transaction,
        )
        database.lessons.delete_many(future, session=transaction)
        database.lesson_series.update_one({"_id": series_id}, {"$set": {
            "active": False, "stopped_at": now(), "blocked_reason": None,
        }}, session=transaction)

    atomic(stop)
    flash("Weekly booking stopped. Future scheduled lessons in this booking were removed.")
    return redirect(url_for("lessons.index"), code=303)


@bp.post("/lessons/plan")
@requires("tutor")
def plan():
    tutor_id = g.user["_id"]
    student_id = object_id(request.form.get("student_id"))
    target = int_field(request.form, "target", "the weekly lesson target", 0, 99)
    topic_id = object_id(request.form["topic_id"]) if request.form.get("topic_id") else None
    week = date_field(request.form.get("week"))
    if tutor_id == student_id:
        raise Problem("Choose a student account.")

    def update(transaction):
        lock_accounts(transaction, {tutor_id: "tutor", student_id: "student"})
        enrollment = enrollment_for(tutor_id, student_id, transaction, topic_id=topic_id, require_topic=True)
        db().enrollments.update_one(
            {"_id": enrollment["_id"]}, {"$set": {"weekly_target": target}}, session=transaction,
        )

    atomic(update)
    flash("Weekly lesson target saved.")
    return redirect(url_for("lessons.index", week=week.isoformat()), code=303)


@bp.get("/lessons/<lesson_id>")
@requires("tutor", "student", "parent")
def detail(lesson_id):
    from .planning import lesson_context
    lesson = get_lesson(lesson_id)
    names = people((lesson["student_id"], lesson["tutor_id"]))
    return render_template(
        "lesson_detail.html", lesson=lesson, names=names, statuses=STATUSES,
        current=now(), local_day=stamp(lesson["start_at"], "input")[:10],
        can_charge=can_charge_cancellation(lesson, now()),
        **lesson_context(lesson),
    )


@bp.get("/lessons/<lesson_id>/cancel")
@requires("parent")
def cancel_form(lesson_id):
    lesson = get_lesson(lesson_id)
    if lesson["status"] != "scheduled" or lesson["start_at"] <= now():
        raise Problem("Only upcoming scheduled lessons can be cancelled.", 409)
    return render_template("lesson_cancel.html", lesson=lesson,
                           consistency_notice=CONSISTENCY_NOTICE, late_notice=PARENT_LATE_NOTICE,
                           late=can_charge_cancellation(lesson, now()))


@bp.post("/lessons/<lesson_id>/cancel")
@requires("parent")
def cancel_by_parent(lesson_id):
    lesson = get_lesson(lesson_id)
    reason = text_field(request.form, "reason", "the cancellation reason", limit=1000)
    if request.form.get("confirm") != "yes":
        raise Problem("Confirm that you would like to cancel this lesson.")

    def cancel(transaction):
        database = db()
        lock_accounts(transaction, {
            lesson["tutor_id"]: "tutor", lesson["student_id"]: "student", g.user["_id"]: "parent",
        })
        student = database.users.find_one({
            "_id": lesson["student_id"], "parent_id": g.user["_id"], "active": True,
        }, session=transaction)
        current = database.lessons.find_one({"_id": lesson["_id"]}, session=transaction)
        timestamp = now()
        if not student:
            abort(404)
        if current["status"] != "scheduled" or current["start_at"] <= timestamp or current.get("invoice_id"):
            raise Problem("This lesson can no longer be cancelled. Reload its details.", 409)
        late = can_charge_cancellation(current, timestamp)
        if late and request.form.get("acknowledge_late") != "yes":
            raise Problem("Acknowledge the late cancellation terms before confirming.", 409)
        database.lessons.update_one({"_id": lesson["_id"]}, {"$set": {
            "status": "cancelled_late" if late else "cancelled", "cancelled_at": timestamp,
            "cancelled_by_parent": g.user["_id"], "cancellation_reason": reason,
            "parent_cancellation_reason": reason,
            "cancellation_fee_pending": True, "charge": False, "billing_parent_id": g.user["_id"],
            "finalized_at": timestamp,
        }}, session=transaction)
        database.change_requests.update_many({"lesson_id": lesson["_id"], "status": "pending"},
            {"$set": {"status": "withdrawn", "resolved_at": timestamp}}, session=transaction)

    atomic(cancel)
    flash("Lesson cancelled. Your tutor will review the cancellation and any applicable fee.")
    return redirect(url_for("lessons.detail", lesson_id=lesson_id), code=303)


@bp.post("/lessons/<lesson_id>/cancellation-fee")
@requires("tutor")
def cancellation_fee(lesson_id):
    lesson = get_lesson(lesson_id)
    decision = request.form.get("decision")
    if decision not in {"charge", "waive"}:
        raise Problem("Choose whether to charge or waive the cancellation fee.")

    def review_fee(transaction):
        lock_accounts(transaction, {lesson["tutor_id"]: "tutor", lesson["student_id"]: "student"})
        current = db().lessons.find_one({"_id": lesson["_id"]}, session=transaction)
        if not current.get("cancellation_fee_pending") or current.get("invoice_id"):
            raise Problem("This cancellation has already been reviewed.", 409)
        charge = decision == "charge"
        if charge and not can_charge_cancellation(current, now()):
            raise Problem("Only cancellations within 24 hours can be charged.")
        db().lessons.update_one({"_id": lesson["_id"]}, {"$set": {
            "charge": charge, "cancellation_fee_pending": False, "cancellation_fee_reviewed_at": now(),
            "cancellation_fee_reviewed_by": g.user["_id"],
        }}, session=transaction)

    atomic(review_fee)
    flash("Cancellation fee charged in full." if decision == "charge" else "Cancellation fee waived.")
    return redirect(url_for("lessons.detail", lesson_id=lesson_id), code=303)


@bp.post("/lessons/<lesson_id>/outcome")
@requires("tutor")
def outcome(lesson_id):
    lesson = get_lesson(lesson_id)
    chosen_status = request.form.get("status")
    if chosen_status not in FINAL_STATUSES:
        raise Problem("Choose a lesson outcome.")
    completed = chosen_status == "completed"
    cancelled = chosen_status.startswith("cancelled")
    parent_feedback, student_feedback = lesson_feedback(request.form) if completed else ("", "")
    reason = text_field(request.form, "reason", "the cancellation reason", limit=1000) if cancelled else ""
    charge_late = cancelled and request.form.get("charge_late") == "yes"

    def update(transaction):
        database = db()
        lock_accounts(transaction, {lesson["tutor_id"]: "tutor", lesson["student_id"]: "student"})
        current = database.lessons.find_one({"_id": lesson["_id"]}, session=transaction)
        if current.get("invoice_id"):
            raise Problem("This lesson is already on an invoice.", 409)
        if current.get("cancellation_fee_pending"):
            raise Problem("Review the parent cancellation fee before changing this outcome.", 409)
        timestamp = now()
        status = chosen_status
        # Tutors may record completion when a lesson is held ahead of its booked time.
        if status == "did_not_attend" and timestamp < current["end_at"]:
            raise Problem("Mark the outcome once the lesson’s scheduled time has ended.")
        cancelled_at = cancellation_time(current, timestamp)
        if status.startswith("cancelled"):
            eligible = can_charge_cancellation(current, timestamp)
            if charge_late and not eligible:
                raise Problem("A cancellation can only be charged when it is made within 24 hours before the lesson.")
            status = "cancelled_late" if eligible else "cancelled"
        student = database.users.find_one({"_id": current["student_id"]}, {"parent_id": 1}, session=transaction)
        database.lessons.update_one(
            {"_id": lesson["_id"]},
            {"$set": {
                "status": status, "parent_feedback": parent_feedback,
                "student_feedback": student_feedback,
                "cancellation_reason": reason if status.startswith("cancelled") else "",
                "cancelled_at": cancelled_at if status.startswith("cancelled") else None,
                "charge": status in {"completed", "did_not_attend"} or (status == "cancelled_late" and charge_late),
                "billing_parent_id": current.get("billing_parent_id") or student.get("parent_id"),
                "finalized_at": current.get("finalized_at", timestamp),
            }, "$unset": {"feedback": ""}},
            session=transaction,
        )
        database.change_requests.update_many(
            {"lesson_id": lesson["_id"], "status": "pending"},
            {"$set": {"status": "withdrawn", "resolved_at": timestamp}},
            session=transaction,
        )

    atomic(update)
    flash("Lesson outcome and feedback saved.")
    return redirect(url_for("lessons.detail", lesson_id=lesson_id), code=303)


@bp.get("/availability")
@requires("tutor", "parent")
def availability():
    child_profiles = children() if g.user["role"] == "parent" else []
    selected_id = object_id(request.args["student_id"]) if request.args.get("student_id") else (
        child_profiles[0]["_id"] if child_profiles else None
    )
    if selected_id and selected_id not in {child["_id"] for child in child_profiles}:
        abort(404)
    owner = availability_owner(g.user["_id"], selected_id) if selected_id else {
        g.user["role"] + "_id": g.user["_id"],
        **({"student_id": {"$exists": False}} if g.user["role"] == "parent" else {}),
    }
    day = date_field(request.args["week"]) if "week" in request.args else today()
    week = day - timedelta(days=day.weekday())
    blocks = list(db().weekly_availability.find(owner).sort([("weekday", 1), ("start_min", 1)]))
    exceptions = list(db().availability_exceptions.find(
        {**owner, **overlaps(boundary(week), boundary(week + timedelta(days=7)))},
    ).sort("start_at", 1))
    return render_template(
        "availability.html", weekdays=WEEKDAYS, blocks=blocks, exceptions=exceptions,
        children=child_profiles, selected_student=selected_id,
        inherited=bool(selected_id and "student_id" in owner and isinstance(owner["student_id"], dict)),
        **availability_calendar(day, blocks, exceptions),
    )


def availability_student(form):
    """Validate the explicit student selection; older clients may edit shared hours."""
    if not form.get("student_id"):
        return None
    if g.user["role"] != "parent":
        abort(404)
    student_id = object_id(form["student_id"])
    if not db().users.find_one({
        "_id": student_id, "parent_id": g.user["_id"], "role": "student", "active": True,
    }, {"_id": 1}):
        abort(404)
    return student_id


def editable_availability_owner(student_id, kind, entry_id, transaction):
    parties = {g.user["_id"]: g.user["role"]}
    if student_id:
        parties[student_id] = "student"
    lock_accounts(transaction, parties)
    if not student_id:
        return {
            g.user["role"] + "_id": g.user["_id"],
            **({"student_id": {"$exists": False}} if g.user["role"] == "parent" else {}),
        }, entry_id
    student = db().users.find_one({
        "_id": student_id, "parent_id": g.user["_id"], "role": "student", "active": True,
    }, {"availability_parent_id": 1}, session=transaction)
    if not student:
        abort(404)
    owner = {"parent_id": g.user["_id"], "student_id": student_id}
    if student.get("availability_parent_id") == g.user["_id"]:
        return owner, entry_id

    # Preserve inherited hours on the first edit without changing either sibling.
    # The marker also remembers a deliberately empty student calendar.
    resolved_id = None
    for entry_kind, collection in (("weekly", db().weekly_availability), ("exception", db().availability_exceptions)):
        for entry in collection.find({"parent_id": g.user["_id"], "student_id": {"$exists": False}}, session=transaction):
            previous_id = entry.pop("_id")
            inserted_id = collection.insert_one({**entry, **owner}, session=transaction).inserted_id
            if entry_kind == kind and previous_id == entry_id:
                resolved_id = inserted_id
    if entry_id and not resolved_id:
        abort(404)
    db().users.update_one({"_id": student_id}, {"$set": {"availability_parent_id": g.user["_id"]}}, session=transaction)
    return owner, resolved_id


@bp.post("/availability/<kind>")
@requires("tutor", "parent")
def add_availability(kind):
    student_id = availability_student(request.form)
    selected_week = date_field(request.form["week"]) if request.form.get("week") else today()
    entry_id = object_id(request.form["entry_id"]) if request.form.get("entry_id") else None
    if kind == "weekly":
        start = time_minutes(request.form.get("start", ""))
        end = time_minutes(request.form.get("end", ""), end=True)
        fields = {
            "weekday": int_field(request.form, "weekday", "a weekday", 0, 6),
            "start_min": start, "end_min": end,
            "kind": request.form.get("kind", "available"),
        }
        if fields["kind"] not in {"available", "preferred"}:
            raise Problem("Choose available or preferred hours.")
        collection = db().weekly_availability
    elif kind == "exception":
        start = local_datetime(request.form.get("starts_at", ""))
        end = local_datetime(request.form.get("ends_at", ""))
        if end <= now():
            raise Problem("Choose an exception that ends in the future.")
        direction = request.form.get("kind")
        if direction not in {"available", "unavailable", "preferred"}:
            raise Problem("Choose available, preferred, or unavailable.")
        fields = {"start_at": start, "end_at": end, "kind": direction}
        collection = db().availability_exceptions
    else:
        abort(404)
    if start >= end:
        raise Problem("The end time must be after the start time.")

    def insert(transaction):
        owner, resolved_id = editable_availability_owner(student_id, kind, entry_id, transaction)
        if entry_id:
            result = collection.update_one(
                {"_id": resolved_id, **owner}, {"$set": fields}, session=transaction,
            )
            if not result.matched_count:
                abort(404)
        else:
            document_owner = {key: value for key, value in owner.items() if not isinstance(value, dict)}
            collection.insert_one({**document_owner, **fields}, session=transaction)

    atomic(insert)
    flash("Availability saved.")
    if kind == "exception" and direction == "unavailable":
        lesson_owner = {"tutor_id": g.user["_id"]} if g.user["role"] == "tutor" else (
            {"student_id": student_id} if student_id else {"student_id": {"$in": [child["_id"] for child in children()]}}
        )
        count = db().lessons.count_documents({**lesson_owner, "status": "scheduled", **overlaps(start, end)})
        if count:
            flash(f"{count} booked lesson{'s overlap' if count != 1 else ' overlaps'} this period. Review the schedule.", "notice")
    return redirect(url_for("lessons.availability", week=selected_week.isoformat(), student_id=student_id), code=303)


@bp.post("/availability/<kind>/<entry_id>/remove")
@requires("tutor", "parent")
def remove_availability(kind, entry_id):
    student_id = availability_student(request.form)
    selected_week = date_field(request.form["week"]) if request.form.get("week") else today()
    collections = {"weekly": db().weekly_availability, "exception": db().availability_exceptions}
    if kind not in collections:
        abort(404)

    def remove(transaction):
        owner, resolved_id = editable_availability_owner(student_id, kind, object_id(entry_id), transaction)
        result = collections[kind].delete_one(
            {"_id": resolved_id, **owner}, session=transaction,
        )
        if not result.deleted_count:
            abort(404)

    atomic(remove)
    flash("Availability removed.")
    return redirect(url_for("lessons.availability", week=selected_week.isoformat(), student_id=student_id), code=303)


def lesson_for_change(lesson_id, transaction):
    database = db()
    lesson = database.lessons.find_one({"_id": lesson_id}, session=transaction)
    if not lesson:
        abort(404)
    student = database.users.find_one(
        {"_id": lesson["student_id"], "active": True, "role": "student"},
        {"parent_id": 1}, session=transaction,
    )
    parent_id = student.get("parent_id") if student else None
    actor_field = g.user["role"] + "_id"
    parties = {"tutor_id": lesson["tutor_id"], "parent_id": parent_id}
    if parties[actor_field] != g.user["_id"]:
        abort(404)
    if not parent_id:
        raise Problem("An administrator must link a parent before you can request a change.")
    lock_accounts(transaction, {
        lesson["student_id"]: "student", lesson["tutor_id"]: "tutor", parent_id: "parent",
    })
    if lesson["status"] != "scheduled" or lesson["start_at"] <= now():
        raise Problem("Only an upcoming lesson can be changed.", 409)
    return lesson, parent_id


@bp.get("/lessons/<lesson_id>/change")
@requires("tutor", "parent")
def change(lesson_id):
    lesson = get_lesson(lesson_id)
    if lesson["status"] != "scheduled" or lesson["start_at"] <= now():
        raise Problem("Only an upcoming lesson can be changed.", 409)
    current = now()
    day = date_field(request.args["date"]) if "date" in request.args else max(
        today(), date_field(stamp(lesson["start_at"], "input")[:10]),
    )
    pending = db().change_requests.find_one({
        "lesson_id": lesson["_id"], "status": "pending", "expires_at": {"$gt": current},
    }, {"_id": 1})
    student = db().users.find_one({"_id": lesson["student_id"]}, {"parent_id": 1})
    parent_id = student.get("parent_id") if student else None
    if not parent_id:
        raise Problem("An administrator must link a parent before you can request a change.")
    return render_template(
        "lesson_change.html", lesson=lesson, day=day, today=today(),
        names=people((lesson["student_id"], lesson["tutor_id"])), pending=pending,
        **change_calendar(lesson, day, parent_id=parent_id),
        late_notice=LATE_NOTICE if g.user["role"] == "parent" and lesson["start_at"] - current < timedelta(hours=24) else "",
    )


@bp.post("/lessons/<lesson_id>/change")
@requires("tutor", "parent")
def request_change(lesson_id):
    lesson_id = object_id(lesson_id)
    desired = local_datetime(request.form.get("starts_at", ""))
    reason = text_field(request.form, "reason", "the reason", limit=1000)
    acknowledged = request.form.get("acknowledge_late") == "yes"

    def insert(transaction):
        database = db()
        lesson, parent_id = lesson_for_change(lesson_id, transaction)
        timestamp = now()
        late = lesson["start_at"] - timestamp < timedelta(hours=24) and g.user["role"] == "parent"
        if late and not acknowledged:
            raise Problem(LATE_NOTICE + " Acknowledge the warning before submitting.")
        if lesson["start_at"] == desired:
            raise Problem("Choose a different lesson time.")
        end = desired + (lesson["end_at"] - lesson["start_at"])
        verify_change_slot(
            lesson["tutor_id"], lesson["student_id"], parent_id, desired, end,
            excluding=lesson_id, session=transaction,
        )
        database.change_requests.update_many(
            {"lesson_id": lesson_id, "status": "pending", "expires_at": {"$lte": timestamp}},
            {"$set": {"status": "expired", "resolved_at": timestamp}}, session=transaction,
        )
        if database.change_requests.find_one({"lesson_id": lesson_id, "status": "pending"}, session=transaction):
            raise Problem("This lesson already has a pending request.", 409)
        database.change_requests.insert_one({
            "lesson_id": lesson_id, "student_id": lesson["student_id"], "tutor_id": lesson["tutor_id"],
            "parent_id": parent_id, "requested_by": g.user["role"], "reason": reason, "late": late,
            "original_start": lesson["start_at"], "start_at": desired, "end_at": end,
            "created_at": timestamp, "expires_at": min(lesson["start_at"], desired), "status": "pending",
        }, session=transaction)

    atomic(insert)
    flash("Change request sent for review.")
    return redirect(url_for("lessons.index"), code=303)


def get_change(change_id, session=None):
    change = db().change_requests.find_one(
        {"_id": object_id(change_id), g.user["role"] + "_id": g.user["_id"]}, session=session,
    )
    if not change:
        abort(404)
    if g.user["role"] == "parent":
        linked = db().users.find_one(
            {"_id": change["student_id"], "parent_id": g.user["_id"], "role": "student", "active": True},
            {"_id": 1}, session=session,
        )
        if not linked:
            abort(404)
    return change


@bp.get("/changes/<change_id>")
@requires("tutor", "parent")
def review(change_id):
    change = get_change(change_id)
    names = people((change["student_id"], change["tutor_id"], change["parent_id"]))
    pending = change["status"] == "pending" and change["expires_at"] > now()
    return render_template("change_review.html", change=change, names=names, pending=pending)


@bp.post("/changes/<change_id>")
@requires("tutor", "parent")
def resolve(change_id):
    action = request.form.get("action")
    if action not in {"approve", "decline", "withdraw"}:
        raise Problem("Choose an action for the request.")
    charge = request.form.get("charge_original") == "yes"

    def update(transaction):
        database = db()
        change = get_change(change_id, transaction)
        is_requester = change["requested_by"] == g.user["role"]
        if is_requester != (action == "withdraw"):
            abort(403)
        if change["status"] != "pending" or change["expires_at"] <= now():
            raise Problem("This request has already closed.", 409)
        lesson, parent_id = lesson_for_change(change["lesson_id"], transaction)
        if parent_id != change["parent_id"] or lesson["start_at"] != change["original_start"]:
            raise Problem("The lesson has changed. Open the schedule to see the current time.", 409)

        status = {"approve": "approved", "decline": "declined", "withdraw": "withdrawn"}[action]
        outcome = {"status": status, "resolved_at": now()}
        if action == "approve":
            verify_change_slot(
                lesson["tutor_id"], lesson["student_id"], parent_id, change["start_at"], change["end_at"],
                excluding=lesson["_id"], session=transaction,
            )
            times = {"start_at": change["start_at"], "end_at": change["end_at"], "updated_at": now()}
            charge_original = charge and change["late"] and g.user["role"] == "tutor"
            if charge_original:
                parent_feedback, student_feedback = lesson_feedback(request.form)
                replacement = database.lessons.insert_one({
                    **{key: lesson[key] for key in ("tutor_id", "student_id", "title", "rate_cents")},
                    **{key: lesson[key] for key in ("topic_id", "topic") if key in lesson},
                    **times, "status": "scheduled", "invoice_id": None, "created_at": now(),
                }, session=transaction)
                database.lessons.update_one(
                    {"_id": lesson["_id"]},
                    {"$set": {
                        "status": "cancelled_late", "charge": True,
                        "parent_feedback": parent_feedback, "student_feedback": student_feedback,
                        "cancelled_at": change["created_at"],
                        "finalized_at": now(), "billing_parent_id": parent_id,
                        "replaced_by": replacement.inserted_id,
                    }, "$unset": {"feedback": ""}}, session=transaction,
                )
                outcome["replacement_id"] = replacement.inserted_id
            else:
                database.lessons.update_one({"_id": lesson["_id"]}, {"$set": times}, session=transaction)
            outcome["charge_original"] = charge_original
        database.change_requests.update_one({"_id": change["_id"]}, {"$set": outcome}, session=transaction)

    atomic(update)
    flash({"approve": "New lesson time confirmed.", "decline": "Change request declined.", "withdraw": "Change request withdrawn."}[action])
    return redirect(url_for("lessons.review", change_id=change_id), code=303)
