from datetime import timedelta
import re

from bson import ObjectId

from portal.core import boundary, local_datetime, now, stamp, today
from portal.scheduling import availability_calendar, open_slots
from test_portal import app, database, users, lesson_record, post, sign_in


def test_both_roles_edit_weekly_and_temporary_calendar_intervals(app, database, users):
    parent = sign_in(app, users["parent"])
    tutor = sign_in(app, users["tutor"])
    with app.app_context():
        day = today() + timedelta(days=8)
        week = day - timedelta(days=day.weekday())
        starts = stamp(boundary(day, 900), "input")
        ends = stamp(boundary(day, 960), "input")
    page = parent.get("/availability", query_string={"week": day})
    assert page.status_code == 200
    assert b'Your repeating weekly availability' in page.data
    assert b'Your temporary availability' in page.data
    assert str(week).encode() in page.data

    assert post(parent, "/availability/weekly", weekday=str(day.weekday()), start="09:15", end="11:45", week=str(day)).status_code == 303
    block = database.weekly_availability.find_one({"parent_id": users["parent"]["_id"], "start_min": 555})
    assert block
    assert post(parent, "/availability/weekly", entry_id=str(block["_id"]), weekday=str(day.weekday()), start="09:30", end="12:00").status_code == 303
    assert database.weekly_availability.find_one({"_id": block["_id"]})["start_min"] == 570
    assert post(tutor, "/availability/weekly", entry_id=str(block["_id"]), weekday="0", start="08:00", end="10:00").status_code == 404
    assert post(tutor, f"/availability/weekly/{block['_id']}/remove").status_code == 404

    assert post(parent, "/availability/exception", kind="available", starts_at=starts, ends_at=ends, week=str(day)).status_code == 303
    exception = database.availability_exceptions.find_one({"parent_id": users["parent"]["_id"]})
    assert post(parent, "/availability/exception", entry_id=str(exception["_id"]), kind="unavailable", starts_at=starts, ends_at=ends).status_code == 303
    assert database.availability_exceptions.find_one({"_id": exception["_id"]})["kind"] == "unavailable"
    page = parent.get("/availability", query_string={"week": day})
    assert str(exception["_id"]).encode() in page.data
    assert post(parent, f"/availability/exception/{exception['_id']}/remove", week=str(day)).status_code == 303
    assert not database.availability_exceptions.find_one({"_id": exception["_id"]})
    assert sign_in(app, users["student"]).get("/availability").status_code == 403


def test_change_calendar_shows_same_mutual_slots_for_parent_and_tutor(app, database, users):
    with app.app_context():
        day = today() + timedelta(days=5)
        original = lesson_record(database, users, "student", boundary(day + timedelta(days=1), 900))
        database.weekly_availability.delete_many({"parent_id": users["parent"]["_id"]})
        database.weekly_availability.insert_one({
            "parent_id": users["parent"]["_id"], "weekday": day.weekday(), "start_min": 540, "end_min": 720,
        })
        database.availability_exceptions.insert_one({
            "parent_id": users["parent"]["_id"], "kind": "unavailable",
            "start_at": boundary(day, 600), "end_at": boundary(day, 630),
        })
        lesson_record(database, users, "student", boundary(day, 540))
        expected = {f"{day}T10:30", f"{day}T10:45", f"{day}T11:00"}
        assert {value for value, _ in open_slots(original, day, parent_id=users["parent"]["_id"])} == expected
    path = f"/lessons/{original['_id']}/change"
    for role in ("parent", "tutor"):
        page = sign_in(app, users[role]).get(path, query_string={"date": day})
        assert page.status_code == 200
        assert b'Times available for both tutor and student' in page.data
        actual = set(re.findall(r'name="starts_at" value="([^"]+)"', page.get_data(as_text=True)))
        assert actual == expected
        assert b'type="radio"' in page.data


def test_no_parent_hours_means_no_mutual_slots_and_forged_change_rejected(app, database, users):
    database.weekly_availability.delete_many({"parent_id": users["parent"]["_id"]})
    with app.app_context():
        day = today() + timedelta(days=4)
        original = lesson_record(database, users, "student", boundary(day, 900))
        desired = stamp(boundary(day + timedelta(days=1), 900), "input")
    path = f"/lessons/{original['_id']}/change"
    for role in ("parent", "tutor"):
        client = sign_in(app, users[role])
        page = client.get(path, query_string={"date": day})
        assert page.status_code == 200
        assert b'No mutual available times this week.' in page.data
        assert post(client, path, starts_at=desired).status_code == 409
    assert database.change_requests.count_documents({"lesson_id": original["_id"]}) == 0


def test_approval_rechecks_parent_hours(app, database, users):
    with app.app_context():
        day = today() + timedelta(days=4)
        original = lesson_record(database, users, "student", boundary(day, 900))
        desired = stamp(boundary(day + timedelta(days=1), 900), "input")
    parent = sign_in(app, users["parent"])
    tutor = sign_in(app, users["tutor"])
    assert post(parent, f"/lessons/{original['_id']}/change", starts_at=desired).status_code == 303
    change = database.change_requests.find_one({"lesson_id": original["_id"]})
    database.weekly_availability.delete_many({"parent_id": users["parent"]["_id"]})
    assert post(tutor, f"/changes/{change['_id']}", action="approve").status_code == 409
    assert database.lessons.find_one({"_id": original["_id"]})["start_at"] == original["start_at"]
    assert database.change_requests.find_one({"_id": change["_id"]})["status"] == "pending"


def test_calendar_retains_exact_times_and_splits_multiday_exceptions(app):
    with app.app_context():
        first = today() + timedelta(days=7)
        first -= timedelta(days=first.weekday())
        block = {"_id": ObjectId(), "weekday": 0, "start_min": 555, "end_min": 1440}
        exception = {"_id": ObjectId(), "kind": "unavailable", "start_at": boundary(first, 1395), "end_at": boundary(first + timedelta(days=1), 45)}
        view = availability_calendar(first, [block], [exception])
    assert view["weekly_entries"][0]["start"] == "09:15"
    assert view["weekly_entries"][0]["end"] == "00:00"
    assert view["weekly_calendar_rows"][18]["cells"][0]["entries"][0]["label"] == "09:15–24:00"
    assert view["temporary_calendar_rows"][46]["cells"][0]["entries"][0]["segment_label"] == "23:15–24:00"
    assert view["temporary_calendar_rows"][0]["cells"][1]["entries"][0]["segment_label"] == "00:00–00:45"
    assert view["temporary_calendar_rows"][1]["cells"][1]["state"] == "unavailable"
    assert view["temporary_calendar_rows"][2]["cells"][1]["state"] == ""


def test_preferred_hours_are_available_and_label_mutual_choices(app, database, users):
    with app.app_context():
        day = today() + timedelta(days=8)
        original = lesson_record(database, users, "student", boundary(day + timedelta(days=1), 900))
    for role in ("tutor", "parent"):
        client = sign_in(app, users[role])
        assert post(client, "/availability/weekly", weekday=str(day.weekday()), start="09:00", end="11:00", kind="preferred").status_code == 303
        page = client.get("/availability", query_string={"week": day})
        assert b"calendar-cell preferred" in page.data
        assert b"data-calendar-remove" in page.data
        assert b"calendar-add" not in page.data
        assert page.data.count(b'rowspan="4"') == 24
        assert post(client, "/availability/weekly", weekday="0", start="09:00", end="11:00", kind="invalid").status_code == 400
    with app.app_context():
        slots = dict(open_slots(original, day, parent_id=users["parent"]["_id"]))
        assert "Both preferred" in slots[f"{day}T09:00"]
        assert "preferred" not in slots[f"{day}T12:00"]
    tutor = sign_in(app, users["tutor"])
    with app.app_context():
        starts, ends = stamp(boundary(day, 600), "input"), stamp(boundary(day, 660), "input")
    assert post(tutor, "/availability/exception", kind="unavailable", starts_at=starts, ends_at=ends).status_code == 303
    with app.app_context():
        assert f"{day}T10:00" not in dict(open_slots(original, day, parent_id=users["parent"]["_id"]))


def test_temporary_preferred_hours_create_availability_and_preserve_ownership(app, database, users):
    database.weekly_availability.delete_many({})
    with app.app_context():
        day = today() + timedelta(days=8)
        starts, ends = stamp(boundary(day, 555), "input"), stamp(boundary(day, 675), "input")
        original = lesson_record(database, users, "student", boundary(day + timedelta(days=1), 900))
    parent = sign_in(app, users["parent"])
    tutor = sign_in(app, users["tutor"])
    for client in (parent, tutor):
        assert post(client, "/availability/exception", kind="preferred", starts_at=starts, ends_at=ends).status_code == 303
    entry = database.availability_exceptions.find_one({"parent_id": users["parent"]["_id"]})
    assert post(tutor, "/availability/exception", entry_id=str(entry["_id"]), kind="preferred", starts_at=starts, ends_at=ends).status_code == 404
    with app.app_context():
        slots = dict(open_slots(original, day, parent_id=users["parent"]["_id"]))
    assert f"{day}T09:15" in slots and "Both preferred" in slots[f"{day}T09:15"]
    assert f"{day}T08:00" not in slots
    assert post(parent, "/availability/exception", entry_id=str(entry["_id"]), kind="unavailable", starts_at=starts, ends_at=ends).status_code == 303
    with app.app_context():
        assert open_slots(original, day, parent_id=users["parent"]["_id"]) == []


def test_parent_saves_independent_student_calendars_and_booking_rechecks_selection(app, database, users):
    parent = sign_in(app, users["parent"])
    tutor = sign_in(app, users["tutor"])
    with app.app_context():
        day = today() + timedelta(days=8)
    inherited = database.weekly_availability.find_one({"parent_id": users["parent"]["_id"], "weekday": day.weekday()})
    for child, start, end in (("student", "09:00", "11:00"), ("sibling", "13:00", "15:00")):
        response = post(parent, "/availability/weekly", student_id=str(users[child]["_id"]),
                        entry_id=str(inherited["_id"]), weekday=str(day.weekday()), start=start, end=end)
        assert response.status_code == 303
        assert f"student_id={users[child]['_id']}" in response.location
        page = parent.get(response.location)
        assert f'name="student_id" value="{users[child]["_id"]}"'.encode() in page.data
    assert database.weekly_availability.find_one({"_id": inherited["_id"]})["start_min"] == 0
    assert database.weekly_availability.count_documents({"parent_id": users["parent"]["_id"], "student_id": users["student"]["_id"]}) == 7
    assert database.weekly_availability.count_documents({"parent_id": users["parent"]["_id"], "student_id": users["sibling"]["_id"]}) == 7
    for child, label in (("student", "09:00–11:00"), ("sibling", "13:00–15:00")):
        snapshot = tutor.get("/lessons/booking-availability", query_string={
            "student_id": str(users[child]["_id"]), "day": str(day), "minutes": "60", "repeat": "once",
        }).get_json()
        assert snapshot["student"]["available"] == [label]
        assert all(("T09:" in value or "T10:" in value) if child == "student" else ("T13:" in value or "T14:" in value)
                   for value, _ in snapshot["slots"])
    form = {"title": "Maths", "starts_at": f"{day}T09:00", "minutes": "60", "repeat": "once"}
    assert post(tutor, "/lessons/new", student_id=str(users["sibling"]["_id"]), **form).status_code == 409
    assert post(tutor, "/lessons/new", student_id=str(users["student"]["_id"]), **form).status_code == 303


def test_student_temporary_exception_does_not_block_sibling_and_checks_ownership(app, database, users):
    parent = sign_in(app, users["parent"])
    tutor = sign_in(app, users["tutor"])
    with app.app_context():
        day = today() + timedelta(days=8)
        original = {"tutor_id": users["tutor"]["_id"], "student_id": users["student"]["_id"],
                    "start_at": boundary(day + timedelta(days=1)), "end_at": boundary(day + timedelta(days=1), 60)}
    form = {"kind": "unavailable", "starts_at": f"{day}T09:00", "ends_at": f"{day}T10:00"}
    assert post(parent, "/availability/exception", student_id=str(users["student"]["_id"]), **form).status_code == 303
    entry = database.availability_exceptions.find_one({"student_id": users["student"]["_id"]})
    with app.app_context():
        assert f"{day}T09:00" not in dict(open_slots(original, day, parent_id=users["parent"]["_id"]))
        sibling = {**original, "student_id": users["sibling"]["_id"]}
        assert f"{day}T09:00" in dict(open_slots(sibling, day, parent_id=users["parent"]["_id"]))
    assert post(parent, "/availability/exception", student_id=str(users["sibling"]["_id"]), entry_id=str(entry["_id"]), **form).status_code == 404
    assert post(parent, f"/availability/exception/{entry['_id']}/remove", student_id=str(users["sibling"]["_id"])).status_code == 404
    assert post(parent, "/availability/exception", student_id=str(users["other_student"]["_id"]), **form).status_code == 404
    assert parent.get("/availability", query_string={"student_id": str(users["other_student"]["_id"])}).status_code == 404
    assert post(tutor, "/availability/exception", student_id=str(users["student"]["_id"]), **form).status_code == 404


def test_empty_individual_student_calendar_does_not_reinherit_family_hours(app, database, users):
    parent = sign_in(app, users["parent"])
    student_id = users["student"]["_id"]
    # Customizing and removing all copied hours is a deliberate empty calendar.
    assert post(parent, "/availability/weekly", student_id=str(student_id), weekday="0", start="09:00", end="10:00").status_code == 303
    for block in database.weekly_availability.find({"student_id": student_id}):
        assert post(parent, f"/availability/weekly/{block['_id']}/remove", student_id=str(student_id)).status_code == 303
    assert database.weekly_availability.count_documents({"student_id": student_id}) == 0
    with app.app_context():
        day = today() + timedelta(days=8)
        original = {"tutor_id": users["tutor"]["_id"], "student_id": student_id,
                    "start_at": boundary(day + timedelta(days=1)), "end_at": boundary(day + timedelta(days=1), 60)}
        assert open_slots(original, day, parent_id=users["parent"]["_id"]) == []
        assert open_slots({**original, "student_id": users["sibling"]["_id"]}, day, parent_id=users["parent"]["_id"])


def test_default_temporary_hour_survives_midnight_and_saves_without_edits(app, database, users, monkeypatch):
    with app.app_context():
        day = today() + timedelta(days=8)
        fixed_now = boundary(day, 23 * 60 + 50)
        monkeypatch.setattr("portal.scheduling.now", lambda: fixed_now)
        context = availability_calendar(day, [], [])
        start = local_datetime(context["temporary_default_start"])
        end = local_datetime(context["temporary_default_end"])
        assert end - start == timedelta(hours=1)
        last_cell = context["temporary_calendar_rows"][-1]["cells"][day.weekday()]
        assert local_datetime(last_cell["ends_at"]) - local_datetime(last_cell["starts_at"]) == timedelta(hours=1)
    for role, kind in (("parent", "available"), ("tutor", "unavailable")):
        client = sign_in(app, users[role])
        assert post(client, "/availability/exception", kind=kind, starts_at=context["temporary_default_start"],
                    ends_at=context["temporary_default_end"]).status_code == 303
