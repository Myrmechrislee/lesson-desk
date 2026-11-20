from datetime import datetime, time, timedelta

from .core import Problem, boundary, clock_label, date_field, db, local_datetime, now, schedule_zone, stamp, zone


def overlaps(start, end):
    return {"start_at": {"$lt": end}, "end_at": {"$gt": start}}


def availability_owner(parent_id, student_id, session=None):
    """A child's own hours replace inherited family hours, including an empty calendar."""
    student = db().users.find_one(
        {"_id": student_id, "parent_id": parent_id, "role": "student", "active": True},
        {"availability_parent_id": 1}, session=session,
    )
    if student and student.get("availability_parent_id") == parent_id:
        return {"parent_id": parent_id, "student_id": student_id}
    return {"parent_id": parent_id, "student_id": {"$exists": False}}


def weekly_intervals(block, start, end):
    """Expand a weekly rule in its saved timezone, returning UTC instants."""
    tz = schedule_zone(block)
    if block.get("start_at") and block.get("end_at"):
        first = block["start_at"].astimezone(tz)
        last = block["end_at"].astimezone(tz)
        weekday = first.weekday()
        start_min = first.hour * 60 + first.minute
        end_min = (last.date() - first.date()).days * 1440 + last.hour * 60 + last.minute
    else:
        weekday, start_min, end_min = block["weekday"], block["start_min"], block["end_min"]
    day = start.astimezone(tz).date() - timedelta(days=1)
    last_day = end.astimezone(tz).date()
    while day <= last_day:
        if day.weekday() == weekday:
            try:
                left = local_datetime(_wall_input(day, start_min), tz=tz)
                right = local_datetime(_wall_input(day, end_min), tz=tz)
            except Problem:
                # A wall time in a skipped or repeated DST hour is not bookable.
                day += timedelta(days=1)
                continue
            if left < end and right > start:
                yield left, right
        day += timedelta(days=1)


def weekly_windows(owner_id, start, end, session=None, *, owner_field="tutor_id", preferred_only=False, student_id=None):
    database = db()
    query = availability_owner(owner_id, student_id, session) if owner_field == "parent_id" and student_id else {owner_field: owner_id}
    if owner_field == "parent_id" and not student_id:
        query["student_id"] = {"$exists": False}
    if preferred_only:
        query["kind"] = "preferred"
    blocks = list(database.weekly_availability.find(query, session=session))
    exception_query = {key: value for key, value in query.items() if key != "kind"}
    exceptions = list(database.availability_exceptions.find(
        {**exception_query, **overlaps(start, end)}, session=session,
    ))
    available_kinds = {"preferred"} if preferred_only else {"available", "preferred"}
    available = [(entry["start_at"], entry["end_at"]) for entry in exceptions if entry["kind"] in available_kinds]
    unavailable = [(entry["start_at"], entry["end_at"]) for entry in exceptions if entry["kind"] == "unavailable"]
    for block in blocks:
        available.extend(weekly_intervals(block, start, end))
    return sorted(available), unavailable


def fits_availability(start, end, windows):
    available, unavailable = windows
    if any(left < end and right > start for left, right in unavailable):
        return False
    covered_until = start
    for left, right in available:
        if left > covered_until:
            break
        covered_until = max(covered_until, right)
        if covered_until >= end:
            return True
    return False


def occupied(tutor_id, student_id, start, end, *, excluding=None, excluding_series=None, session=None):
    query = {
        "$or": [{"tutor_id": tutor_id}, {"student_id": student_id}],
        "status": "scheduled",
        **overlaps(start, end),
    }
    if excluding:
        query["_id"] = {"$ne": excluding}
    if excluding_series:
        query["series_id"] = {"$ne": excluding_series}
    yield from db().lessons.find(query, {"start_at": 1, "end_at": 1, "tutor_id": 1, "student_id": 1}, session=session)
    # Ongoing bookings also reserve weeks beyond the materialized calendar.
    series_query = {"active": True, "$or": [{"tutor_id": tutor_id}, {"student_id": student_id}]}
    if excluding_series:
        series_query["_id"] = {"$ne": excluding_series}
    for series in db().lesson_series.find(series_query, session=session):
        tz = schedule_zone(series)
        day = start.astimezone(tz).date() - timedelta(days=1)
        last = end.astimezone(tz).date()
        first = date_field(series["first_on"])
        while day <= last:
            if day >= first and day.weekday() == first.weekday():
                try:
                    left = local_datetime(f"{day}T{series['time']}", tz=tz)
                except Problem:
                    day += timedelta(days=1)
                    continue
                right = left + timedelta(minutes=series["minutes"])
                if left < end and right > start and not db().lessons.find_one({
                    "series_id": series["_id"], "occurrence_on": day.isoformat(),
                }, {"_id": 1}, session=session):
                    yield {"start_at": left, "end_at": right, "tutor_id": series["tutor_id"], "student_id": series["student_id"]}
            day += timedelta(days=1)


def verify_slot(tutor_id, student_id, start, end, *, excluding=None, excluding_series=None, skip_availability=False, session):
    if start <= now():
        raise Problem("Choose a lesson time in the future.")
    if not skip_availability and not fits_availability(start, end, weekly_windows(tutor_id, start, end, session)):
        raise Problem("That time is outside the tutor’s availability. Choose another time.", 409)
    student = db().users.find_one({"_id": student_id, "role": "student", "active": True}, {"parent_id": 1}, session=session)
    parent_id = student.get("parent_id") if student else None
    if not parent_id:
        raise Problem("An administrator must link a parent before scheduling lessons.", 409)
    if not skip_availability and not fits_availability(start, end, weekly_windows(parent_id, start, end, session, owner_field="parent_id", student_id=student_id)):
        raise Problem("That time is outside this student’s availability set by their parent. Choose a time when both the tutor and student are available.", 409)
    if next(occupied(tutor_id, student_id, start, end, excluding=excluding, excluding_series=excluding_series, session=session), None):
        raise Problem("The tutor or student already has a lesson at that time.", 409)


def verify_change_slot(tutor_id, student_id, parent_id, start, end, *, excluding=None, session):
    verify_slot(tutor_id, student_id, start, end, excluding=excluding, session=session)
    if not fits_availability(start, end, weekly_windows(
        parent_id, start, end, session, owner_field="parent_id", student_id=student_id,
    )):
        raise Problem("That time is outside this student’s availability set by their parent. Choose a time when both the tutor and student are available.", 409)


def open_slots(lesson, day, *, parent_id=None):
    duration = lesson["end_at"] - lesson["start_at"]
    start_of_day = boundary(day)
    end_of_day = boundary(day + timedelta(days=1))
    windows = weekly_windows(lesson["tutor_id"], start_of_day, end_of_day + duration)
    parent_windows = weekly_windows(
        parent_id, start_of_day, end_of_day + duration, owner_field="parent_id", student_id=lesson["student_id"],
    ) if parent_id is not None else None
    tutor_preferred = weekly_windows(lesson["tutor_id"], start_of_day, end_of_day + duration, preferred_only=True)
    parent_preferred = weekly_windows(
        parent_id, start_of_day, end_of_day + duration, owner_field="parent_id", preferred_only=True, student_id=lesson["student_id"],
    ) if parent_id is not None else None
    bookings = list(occupied(
        lesson["tutor_id"], lesson["student_id"], start_of_day, end_of_day + duration,
        excluding=lesson.get("_id"),
    ))
    wall_times = {stamp(left, "input") for left, _ in windows[0] if start_of_day <= left < end_of_day}
    if parent_windows is not None:
        wall_times.update(stamp(left, "input") for left, _ in parent_windows[0] if start_of_day <= left < end_of_day)
    wall_times.update(f"{day.isoformat()}T{minute // 60:02d}:{minute % 60:02d}" for minute in range(0, 1440, 15))
    candidates = []
    for local in wall_times:
        try:
            candidates.append(local_datetime(local))
        except Problem:
            continue
    slots = []
    current = now()
    for start in sorted(candidates):
        end = start + duration
        if start <= current or start == lesson.get("start_at"):
            continue
        if not fits_availability(start, end, windows):
            continue
        if parent_windows is not None and not fits_availability(start, end, parent_windows):
            continue
        if any(row["start_at"] < end and row["end_at"] > start for row in bookings):
            continue
        preferred_by = []
        if fits_availability(start, end, tutor_preferred):
            preferred_by.append("Tutor")
        if parent_preferred is not None and fits_availability(start, end, parent_preferred):
            preferred_by.append("Student")
        preference = " · Both preferred" if len(preferred_by) == 2 else (
            f" · {preferred_by[0]} preferred" if preferred_by else ""
        )
        slots.append((stamp(start, "input"), f"{stamp(start, 'time')}–{stamp(end, 'time')}{preference}"))
    return slots


def calendar_week(day):
    """Use the same Monday-based week in every availability view."""
    first = day - timedelta(days=day.weekday())
    return {
        "calendar_week": first,
        "previous_week": first - timedelta(days=7),
        "next_week": first + timedelta(days=7),
        "last_day": first + timedelta(days=6),
        "calendar_days": [first + timedelta(days=offset) for offset in range(7)],
    }


def _clock(minute):
    return f"{minute // 60 % 24:02d}:{minute % 60:02d}"


def _wall_input(day, minute):
    return (datetime.combine(day, time.min) + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M")


def availability_calendar(day, blocks, exceptions):
    """Prepare half-hour calendar cells while retaining each entry's exact times.

    A cell is shaded when any part is covered. The entry labels and editor retain
    minute precision, including exceptions spanning midnight or multiple days.
    """
    context = calendar_week(day)
    weekly_entries, weekly_segments = [], [[] for _ in range(7)]
    week_start, week_end = boundary(context["calendar_week"]), boundary(context["next_week"])
    for block in blocks:
        seen = False
        for left, right in weekly_intervals(block, week_start, week_end):
            local_start, local_end = left.astimezone(zone()), right.astimezone(zone())
            start_min = local_start.hour * 60 + local_start.minute
            end_min = (local_end.date() - local_start.date()).days * 1440 + local_end.hour * 60 + local_end.minute
            label = f"{clock_label(start_min)}–{clock_label(end_min if end_min <= 1440 else end_min % 1440)}"
            if end_min > 1440:
                label += " (+1 day)"
            if schedule_zone(block) != zone():
                label += f" · repeats in {schedule_zone(block).key}"
            entry = {
                "id": str(block["_id"]), "weekday": local_start.weekday(),
                "start": _clock(start_min), "end": _clock(end_min),
                "end_next_day": "yes" if local_end.date() > local_start.date() else "no",
                "start_min": start_min, "end_min": end_min,
                "label": label, "kind": block.get("kind", "available"),
            }
            if not seen:
                weekly_entries.append(entry)
                seen = True
            for weekday, calendar_day in enumerate(context["calendar_days"]):
                day_start, day_end = boundary(calendar_day), boundary(calendar_day + timedelta(days=1))
                if left < day_end and right > day_start:
                    clipped_start, clipped_end = max(left, day_start).astimezone(zone()), min(right, day_end).astimezone(zone())
                    weekly_segments[weekday].append({**entry,
                        "start_min": clipped_start.hour * 60 + clipped_start.minute,
                        "end_min": 1440 if right >= day_end else clipped_end.hour * 60 + clipped_end.minute,
                    })
    temporary_entries = [{
        "id": str(entry["_id"]), "kind": entry["kind"],
        "starts_at": stamp(entry["start_at"], "input"),
        "ends_at": stamp(entry["end_at"], "input"),
        "label": f"{entry['kind'].capitalize()}: {stamp(entry['start_at'])} → {stamp(entry['end_at'])}",
        "start_at": entry["start_at"], "end_at": entry["end_at"],
    } for entry in exceptions if (
        entry["start_at"] < boundary(context["next_week"])
        and entry["end_at"] > boundary(context["calendar_week"])
    )]
    segments = []
    for calendar_day in context["calendar_days"]:
        start, end = boundary(calendar_day), boundary(calendar_day + timedelta(days=1))
        entries = []
        for entry in temporary_entries:
            if entry["start_at"] >= end or entry["end_at"] <= start:
                continue
            clipped_start = max(start, entry["start_at"])
            clipped_end = min(end, entry["end_at"])
            local_start, local_end = clipped_start.astimezone(zone()), clipped_end.astimezone(zone())
            entries.append({
                **entry,
                "start_min": local_start.hour * 60 + local_start.minute,
                "end_min": 1440 if clipped_end == end else local_end.hour * 60 + local_end.minute,
                "segment_label": f"{stamp(clipped_start, 'time')}–{clock_label(1440) if clipped_end == end else stamp(clipped_end, 'time')}",
            })
        segments.append(entries)

    weekly_rows, temporary_rows = [], []
    for minute in range(0, 1440, 30):
        weekly_cells, temporary_cells = [], []
        for weekday, calendar_day in enumerate(context["calendar_days"]):
            weekly = weekly_segments[weekday]
            weekly_active = [entry for entry in weekly if entry["start_min"] < minute + 30 and entry["end_min"] > minute]
            recurring = bool(weekly_active)
            preferred = any(entry["kind"] == "preferred" for entry in weekly_active)
            active = [entry for entry in segments[weekday] if entry["start_min"] < minute + 30 and entry["end_min"] > minute]
            # Prefer the most informative overlapping interval when a cell is selected.
            weekly_active.sort(key=lambda entry: entry["kind"] != "preferred")
            active.sort(key=lambda entry: {"unavailable": 0, "preferred": 1, "available": 2}[entry["kind"]])
            weekly_cells.append({
                "weekday": weekday, "start": _clock(minute), "end": _clock(min(minute + 60, 1440)),
                "state": "preferred" if preferred else ("available" if recurring else ""),
                "entry_id": weekly_active[0]["id"] if weekly_active else "",
                "entries": [entry for entry in weekly if minute <= entry["start_min"] < minute + 30],
            })
            state = "unavailable" if any(entry["kind"] == "unavailable" for entry in active) else (
                "preferred" if preferred or any(entry["kind"] == "preferred" for entry in active) else (
                    "available" if active else ("recurring" if recurring else "")
                )
            )
            temporary_cells.append({
                "date": calendar_day, "starts_at": _wall_input(calendar_day, minute),
                "ends_at": _wall_input(calendar_day, minute + 60), "state": state,
                "entry_id": active[0]["id"] if active else "",
                "entries": [entry for entry in segments[weekday] if minute <= entry["start_min"] < minute + 30],
            })
        weekly_rows.append({"label": clock_label(minute), "clock": _clock(minute), "show_label": minute % 120 == 0, "cells": weekly_cells})
        temporary_rows.append({"label": clock_label(minute), "clock": _clock(minute), "show_label": minute % 120 == 0, "cells": temporary_cells})
    context.update(
        weekly_calendar_rows=weekly_rows, temporary_calendar_rows=temporary_rows,
        weekly_entries=weekly_entries, temporary_entries=temporary_entries,
    )
    local_now = now().astimezone(zone())
    default_day = local_now.date() if context["calendar_week"] <= local_now.date() <= context["last_day"] else context["calendar_week"]
    default_minute = max(540, (local_now.hour * 60 + local_now.minute) // 30 * 30 + 30) if default_day == local_now.date() else 540
    context.update(
        temporary_default_start=_wall_input(default_day, default_minute),
        temporary_default_end=_wall_input(default_day, default_minute + 60),
    )
    return context


def change_calendar(lesson, day, *, parent_id=None):
    """Only expose start times that fit the lesson and pass booking checks."""
    context = calendar_week(day)
    daily_slots = [open_slots(lesson, calendar_day, parent_id=parent_id) for calendar_day in context["calendar_days"]]
    bookings = list(occupied(
        lesson["tutor_id"], lesson["student_id"], boundary(context["calendar_week"]),
        boundary(context["next_week"]), excluding=lesson.get("_id"),
    ))
    hours = [int(value[11:13]) for slots in daily_slots for value, _ in slots]
    busy_cells = {}
    for weekday, calendar_day in enumerate(context["calendar_days"]):
        for hour in range(24):
            left, right = boundary(calendar_day, hour * 60), boundary(calendar_day, (hour + 1) * 60)
            if right <= left:  # A spring daylight-saving transition can skip an hour.
                busy_cells[weekday, hour] = []
                continue
            labels = sorted({
                f"{stamp(max(row['start_at'], left), 'time')}–{clock_label(1440) if min(row['end_at'], right) == boundary(calendar_day + timedelta(days=1)) else stamp(min(row['end_at'], right), 'time')}"
                for row in bookings if row["start_at"] < right and row["end_at"] > left
            })
            if labels:
                hours.append(hour)
            busy_cells[weekday, hour] = labels
    rows = []
    for hour in range(min(hours), max(hours) + 1) if hours else range(8, 20):
        rows.append({
            "label": clock_label(hour * 60),
            "cells": [[(value, label) for value, label in slots if int(value[11:13]) == hour] for slots in daily_slots],
            "booked": [busy_cells[weekday, hour] for weekday in range(7)],
        })
    context.update(calendar_hours=rows, has_slots=any(daily_slots), has_bookings=bool(bookings))
    return context
