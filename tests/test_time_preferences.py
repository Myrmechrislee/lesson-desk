from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, g
import pytest

from portal.core import Problem, boundary, ensure_indexes, local_datetime, stamp
from portal.lesson_reminders import current_message
from portal.recurring import HORIZON_WEEKS, maintain_series
from portal.scheduling import availability_calendar, fits_availability, weekly_windows
from test_portal import app, database, users, lesson_record, post, sign_in


def set_preferences(database, user, timezone, time_format="12"):
    database.users.update_one({"_id": user["_id"]}, {"$set": {
        "preferences": {"timezone": timezone, "time_format": time_format},
    }})


@pytest.mark.parametrize("timezone, clock, wall", [
    ("Australia/Melbourne", "2:30 AM", "2030-01-16T02:30"),
    ("Asia/Shanghai", "11:30 PM", "2030-01-15T23:30"),
])
def test_local_clock_round_trip_and_utc_storage(timezone, clock, wall):
    application = Flask(__name__)
    application.extensions["zone"] = ZoneInfo("UTC")
    with application.test_request_context():
        g.user = {"preferences": {"timezone": timezone}}
        instant = datetime(2030, 1, 15, 15, 30, tzinfo=UTC)
        assert stamp(instant, "time") == clock
        assert stamp(instant, "input") == wall
        assert local_datetime(wall) == instant
        assert local_datetime(wall).tzinfo == UTC
        g.user["preferences"]["time_format"] = "24"
        assert stamp(instant, "time") == wall[11:]
        assert stamp(instant, "input") == wall


def test_melbourne_dst_rejects_skipped_and_repeated_times():
    application = Flask(__name__)
    application.extensions["zone"] = ZoneInfo("Australia/Melbourne")
    with application.app_context():
        for wall in ("2030-10-06T02:30", "2030-04-07T02:30"):
            with pytest.raises(Problem):
                local_datetime(wall)
        assert local_datetime("2030-10-06T03:30") == datetime(2030, 10, 5, 16, 30, tzinfo=UTC)


@pytest.mark.parametrize("role", ["admin", "tutor", "parent", "student"])
def test_settings_defaults_to_12_hours_and_saves_for_every_role(app, database, users, role):
    client = sign_in(app, users[role])
    page = client.get("/settings")
    assert page.status_code == 200
    assert b'value="12" selected' in page.data
    assert b"Australia/Melbourne" in page.data and b"Asia/Shanghai" in page.data
    assert post(client, "/settings", timezone="Australia/Melbourne", time_format="24").status_code == 303
    saved = database.users.find_one({"_id": users[role]["_id"]})
    assert saved["preferences"] == {"timezone": "Australia/Melbourne", "time_format": "24"}
    page = client.get("/settings")
    assert b'value="24" selected' in page.data
    assert b'value="Australia/Melbourne" selected' in page.data
    assert b"data-detect-timezone-url" not in page.data
    assert b'value="24" selected' in sign_in(app, saved).get("/settings").data


@pytest.mark.parametrize("timezone, time_format", [("Not/A_Zone", "12"), ("Asia/Shanghai", "13"), ("", "24")])
def test_invalid_preferences_are_rejected_without_changing_account(app, database, users, timezone, time_format):
    client = sign_in(app, users["tutor"])
    assert post(client, "/settings", timezone=timezone, time_format=time_format).status_code == 400
    assert "preferences" not in database.users.find_one({"_id": users["tutor"]["_id"]})


def test_device_timezone_initializes_once_and_preferences_are_private(app, database, users):
    tutor = sign_in(app, users["tutor"])
    assert tutor.post("/settings/detect-timezone", data={"timezone": "Australia/Melbourne"}).status_code == 400
    assert post(tutor, "/settings/detect-timezone", timezone="Australia/Melbourne").json == {"updated": True}
    assert post(tutor, "/settings", timezone="Asia/Shanghai", time_format="24").status_code == 303
    assert post(tutor, "/settings/detect-timezone", timezone="Australia/Melbourne").json == {"updated": False}
    assert database.users.find_one({"_id": users["tutor"]["_id"]})["preferences"]["timezone"] == "Asia/Shanghai"
    assert "preferences" not in database.users.find_one({"_id": users["parent"]["_id"]})
    assert app.test_client().get("/settings").status_code == 302


def test_same_lesson_displays_each_viewers_date_time_and_clock_format(app, database, users):
    lesson = lesson_record(database, users, "student", datetime(2030, 1, 15, 15, 30, tzinfo=UTC))
    set_preferences(database, users["tutor"], "Australia/Melbourne")
    set_preferences(database, users["parent"], "Asia/Shanghai")
    tutor = sign_in(app, users["tutor"])
    parent = sign_in(app, users["parent"])
    path = f"/lessons/{lesson['_id']}"
    assert b"2:30 AM" in tutor.get(path).data
    assert b"11:30 PM" in parent.get(path).data
    assert post(parent, "/settings", timezone="Asia/Shanghai", time_format="24").status_code == 303
    assert b"23:30" in parent.get(path).data
    assert database.lessons.find_one({"_id": lesson["_id"]})["start_at"] == lesson["start_at"]


def test_temporary_availability_and_booking_parse_the_selected_timezone(app, database, users):
    set_preferences(database, users["tutor"], "Australia/Melbourne")
    set_preferences(database, users["parent"], "Asia/Shanghai")
    tutor = sign_in(app, users["tutor"])
    assert post(tutor, "/availability/exception", kind="preferred",
                starts_at="2030-01-16T02:30", ends_at="2030-01-16T03:30").status_code == 303
    exception = database.availability_exceptions.find_one({"tutor_id": users["tutor"]["_id"]})
    assert exception["start_at"] == datetime(2030, 1, 15, 15, 30, tzinfo=UTC)
    assert exception["start_at"].utcoffset() == timedelta(0)
    assert post(tutor, "/lessons/new", student_id=str(users["student"]["_id"]),
                starts_at="2030-01-16T02:30", title="Local lesson", minutes="60", repeat="once").status_code == 303
    lesson = database.lessons.find_one({})
    assert lesson["start_at"] == exception["start_at"]
    assert post(tutor, "/settings", timezone="Asia/Shanghai", time_format="12").status_code == 303
    page = tutor.get("/availability?week=2030-01-15")
    assert b'data-starts-at="2030-01-15T23:30"' in page.data
    assert b"11:30 PM" in page.data


def test_weekly_hours_keep_source_timezone_and_shift_across_dst_for_shanghai_viewer(app, database, users):
    database.weekly_availability.delete_many({})
    set_preferences(database, users["tutor"], "Australia/Melbourne")
    tutor = sign_in(app, users["tutor"])
    assert post(tutor, "/availability/weekly", weekday="6", start="10:00", end="11:00",
                week="2030-09-29").status_code == 303
    block = database.weekly_availability.find_one({})
    assert block["timezone"] == "Australia/Melbourne"
    assert block["start_at"] == datetime(2030, 9, 29, 0, tzinfo=UTC)
    assert post(tutor, "/settings", timezone="Asia/Shanghai", time_format="12").status_code == 303
    for day, expected in ((date(2030, 9, 29), "8:00 AM"), (date(2030, 10, 6), "7:00 AM")):
        assert expected.encode() in tutor.get("/availability", query_string={"week": str(day)}).data
        with app.test_request_context():
            g.user = database.users.find_one({"_id": users["tutor"]["_id"]})
            windows = weekly_windows(users["tutor"]["_id"], boundary(day), boundary(day + timedelta(days=1)))
            start = datetime(2030, 9, 29, 0, tzinfo=UTC) if day.month == 9 else datetime(2030, 10, 5, 23, tzinfo=UTC)
            assert fits_availability(start, start + timedelta(hours=1), windows)
            assert not fits_availability(start + timedelta(hours=1), start + timedelta(hours=2), windows)


def test_cross_midnight_weekly_hours_render_and_edit_in_local_timezone(app, database, users):
    database.weekly_availability.delete_many({})
    set_preferences(database, users["tutor"], "Asia/Shanghai")
    tutor = sign_in(app, users["tutor"])
    assert post(tutor, "/availability/weekly", weekday="0", start="22:00", end="23:45", week="2030-01-14").status_code == 303
    block = database.weekly_availability.find_one({})
    assert post(tutor, "/settings", timezone="Australia/Melbourne", time_format="12").status_code == 303
    page = tutor.get("/availability?week=2030-01-14")
    assert b'data-weekday="1" data-start="01:00" data-end="02:45"' in page.data
    assert post(tutor, "/availability/weekly", entry_id=str(block["_id"]), weekday="1",
                start="23:00", end="01:00", week="2030-01-14").status_code == 303
    block = database.weekly_availability.find_one({})
    assert block["end_at"] - block["start_at"] == timedelta(hours=2)
    page = tutor.get("/availability?week=2030-01-14")
    assert b"11:00 PM" in page.data and b"(+1 day)" in page.data


def test_full_day_weekly_hours_remain_editable_after_timezone_conversion(app, database, users):
    set_preferences(database, users["tutor"], "Australia/Melbourne")
    tutor = sign_in(app, users["tutor"])
    block = database.weekly_availability.find_one({"tutor_id": users["tutor"]["_id"], "weekday": 1})
    page = tutor.get("/availability?week=2030-01-15")
    assert b'data-weekday="1" data-start="03:00" data-end="03:00"' in page.data
    assert post(tutor, "/availability/weekly", entry_id=str(block["_id"]), weekday="1", start="03:00",
                end="03:00", kind="preferred", week="2030-01-15").status_code == 303
    saved = database.weekly_availability.find_one({"_id": block["_id"]})
    assert saved["end_at"] - saved["start_at"] == timedelta(days=1)
    assert saved["timezone"] == "Australia/Melbourne"


def test_25_hour_availability_across_dst_keeps_its_end_date_when_edited_in_shanghai(app, database, users):
    database.weekly_availability.delete_many({})
    set_preferences(database, users["tutor"], "Australia/Melbourne")
    tutor = sign_in(app, users["tutor"])
    assert post(tutor, "/availability/weekly", weekday="6", start="00:00", end="00:00",
                week="2030-04-07").status_code == 303
    block = database.weekly_availability.find_one({})
    assert block["end_at"] - block["start_at"] == timedelta(hours=25)
    assert post(tutor, "/settings", timezone="Asia/Shanghai", time_format="12").status_code == 303
    page = tutor.get("/availability?week=2030-04-07")
    assert b'data-weekday="5" data-start="21:00" data-end="22:00" data-end-next-day="yes"' in page.data
    assert post(tutor, "/availability/weekly", entry_id=str(block["_id"]), weekday="5", start="21:00",
                end="22:00", end_next_day="yes", week="2030-04-07").status_code == 303
    saved = database.weekly_availability.find_one({})
    assert (saved["start_at"], saved["end_at"]) == (block["start_at"], block["end_at"])


def test_background_extension_preserves_melbourne_wall_time_after_preference_change(app, database, users):
    set_preferences(database, users["tutor"], "Australia/Melbourne")
    tutor = sign_in(app, users["tutor"])
    assert post(tutor, "/lessons/new", student_id=str(users["student"]["_id"]),
                starts_at="2030-09-29T10:00", minutes="60", title="Melbourne weekly", repeat="forever").status_code == 303
    series = database.lesson_series.find_one({})
    assert series["timezone"] == "Australia/Melbourne"
    lessons = list(database.lessons.find({}).sort("start_at", 1))
    assert lessons[1]["start_at"] - lessons[0]["start_at"] == timedelta(hours=167)
    assert post(tutor, "/settings", timezone="Asia/Shanghai", time_format="24").status_code == 303
    with app.app_context():
        maintain_series(scope={"tutor_id": users["tutor"]["_id"]}, until=date(2031, 5, 1))
    lessons = list(database.lessons.find({}))
    assert len(lessons) > HORIZON_WEEKS
    assert {row["start_at"].astimezone(ZoneInfo("Australia/Melbourne")).hour for row in lessons} == {10}
    assert {row["start_at"].hour for row in lessons} == {0, 23}


def test_migration_preserves_legacy_availability_and_series_timezone(app, database, users):
    legacy = database.weekly_availability.find_one({"tutor_id": users["tutor"]["_id"], "weekday": 1})
    series_id = database.lesson_series.insert_one({
        "tutor_id": users["tutor"]["_id"], "student_id": users["student"]["_id"],
        "first_on": "2030-01-15", "time": "10:00", "minutes": 60, "active": False,
    }).inserted_id
    with app.app_context():
        ensure_indexes()
        ensure_indexes()
    saved = database.weekly_availability.find_one({"_id": legacy["_id"]})
    series = database.lesson_series.find_one({"_id": series_id})
    assert saved["timezone"] == series["timezone"] == "Asia/Shanghai"
    assert saved["end_at"] - saved["start_at"] == timedelta(days=1)
    assert series["start_at"] == datetime(2030, 1, 15, 2, tzinfo=UTC)
    set_preferences(database, users["tutor"], "Australia/Melbourne")
    with app.test_request_context():
        g.user = database.users.find_one({"_id": users["tutor"]["_id"]})
        day = date(2030, 1, 15)
        windows = weekly_windows(users["tutor"]["_id"], boundary(day), boundary(day + timedelta(days=1)))
        assert fits_availability(boundary(day, 600), boundary(day, 660), windows)


def test_ongoing_conflicts_across_zones_include_dst_beyond_the_initial_horizon(app, database, users):
    set_preferences(database, users["tutor"], "Australia/Melbourne")
    tutor = sign_in(app, users["tutor"])
    form = {"student_id": str(users["student"]["_id"]), "starts_at": "2030-09-29T10:00",
            "minutes": "60", "title": "Melbourne weekly", "repeat": "forever"}
    assert post(tutor, "/lessons/new", **form).status_code == 303
    assert post(tutor, "/settings", timezone="Asia/Shanghai", time_format="12").status_code == 303
    # 7 AM Shanghai is free during Melbourne's winter, but conflicts with the
    # first booking once Melbourne enters DST, after the initial twelve weeks.
    form.update(student_id=str(users["sibling"]["_id"]), starts_at="2031-05-25T07:00")
    assert post(tutor, "/lessons/new", **form).status_code == 409
    assert database.lesson_series.count_documents({}) == 1


def test_reminders_use_recipient_timezone_and_clock_preferences(app, database, users):
    set_preferences(database, users["parent"], "Australia/Melbourne")
    lesson = lesson_record(database, users, "student", datetime(2030, 1, 15, 15, 30, tzinfo=UTC))
    job = {"kind": "lesson", "user_id": users["parent"]["_id"], "lesson_id": lesson["_id"],
           "start_at": lesson["start_at"], "end_at": lesson["end_at"]}
    with app.app_context():
        _, subject, body = current_message(job, lesson["start_at"] - timedelta(hours=1))
    assert "2:30 AM" in subject and "Australia/Melbourne" in body
