from datetime import date, timedelta

import pytest

from portal.core import boundary, ensure_indexes, now, stamp, today
from portal.recurring import HORIZON_WEEKS, maintain_series
from test_portal import app, database, users, lesson_record, post, sign_in


def booking(app, users, *, day=None, **extra):
    with app.app_context():
        day = day or today() + timedelta(days=3)
        starts_at = stamp(boundary(day, 600), "input")
    return {"student_id": str(users["student"]["_id"]), "starts_at": starts_at,
            "title": "Weekly mathematics", "minutes": "60", **extra}


def test_weekly_booking_defaults_to_ongoing_and_extends_without_duplicates(app, database, users):
    tutor = sign_in(app, users["tutor"])
    assert b'value="forever" selected' in tutor.get("/lessons/new").data
    form = booking(app, users)
    assert post(tutor, "/lessons/new", **form).status_code == 303
    series = database.lesson_series.find_one({})
    assert series["active"]
    assert database.lessons.count_documents({"series_id": series["_id"]}) == HORIZON_WEEKS
    first = database.lessons.find_one({"series_id": series["_id"]}, sort=[("start_at", 1)])
    assert first["topic_id"] == series["topic_id"] and first["rate_cents"] == 6500

    with app.app_context():
        far_week = today() + timedelta(weeks=20)
    assert tutor.get("/lessons", query_string={"week": str(far_week)}).status_code == 200
    count = database.lessons.count_documents({"series_id": series["_id"]})
    assert count > HORIZON_WEEKS
    tutor.get("/lessons", query_string={"week": str(far_week)})
    assert database.lessons.count_documents({"series_id": series["_id"]}) == count

    path = f"/lessons/series/{series['_id']}/stop"
    assert post(sign_in(app, users["other_tutor"]), path).status_code == 404
    assert post(sign_in(app, users["parent"]), path).status_code == 403
    assert post(tutor, path).status_code == 303
    assert not database.lesson_series.find_one({"_id": series["_id"]})["active"]
    assert database.lessons.count_documents({"series_id": series["_id"]}) == 0
    tutor.get("/lessons", query_string={"week": str(far_week)})
    assert database.lessons.count_documents({"series_id": series["_id"]}) == 0


def test_ongoing_booking_reserves_unmaterialized_weeks_and_keeps_cancelled_occurrences(app, database, users):
    tutor = sign_in(app, users["tutor"])
    form = booking(app, users)
    assert post(tutor, "/lessons/new", **form).status_code == 303
    series = database.lesson_series.find_one({})
    original = database.lessons.find_one({"series_id": series["_id"]}, sort=[("start_at", 1)])
    assert post(tutor, f"/lessons/{original['_id']}/outcome", status="cancelled", reason="Away").status_code == 303
    with app.app_context():
        far_day = today() + timedelta(days=3, weeks=60)
        far_form = booking(app, users, day=far_day, repeat="once", student_id=str(users["sibling"]["_id"]))
        assert database.lessons.count_documents({"start_at": boundary(far_day, 600)}) == 0
    assert post(tutor, "/lessons/new", **far_form).status_code == 409
    with app.app_context():
        maintain_series(scope={"tutor_id": users["tutor"]["_id"]}, until=today() + timedelta(weeks=16))
    assert database.lessons.count_documents({"series_id": series["_id"], "occurrence_on": original["occurrence_on"]}) == 1
    assert database.lessons.find_one({"_id": original["_id"]})["status"] == "cancelled"


@pytest.mark.parametrize("future_repeat", ["forever", "once"])
def test_new_ongoing_booking_checks_conflicts_beyond_its_initial_horizon(app, database, users, future_repeat):
    tutor = sign_in(app, users["tutor"])
    with app.app_context():
        future_day = today() + timedelta(days=3, weeks=80)
    future = booking(app, users, day=future_day, repeat=future_repeat)
    assert post(tutor, "/lessons/new", **future).status_code == 303
    current = booking(app, users, student_id=str(users["sibling"]["_id"]))
    assert post(tutor, "/lessons/new", **current).status_code == 409
    preview = tutor.get("/lessons/booking-availability", query_string={**current, "day": current["starts_at"][:10]}).get_json()
    assert not preview["valid"]
    assert database.lesson_series.count_documents({}) == (1 if future_repeat == "forever" else 0)


def test_extension_rechecks_parent_availability_and_recovers_after_fix(app, database, users):
    tutor = sign_in(app, users["tutor"])
    assert post(tutor, "/lessons/new", **booking(app, users)).status_code == 303
    series = database.lesson_series.find_one({})
    with app.app_context():
        next_day = today() + timedelta(days=3, weeks=HORIZON_WEEKS)
        start = boundary(next_day, 600)
        exception_id = database.availability_exceptions.insert_one({
            "parent_id": users["parent"]["_id"], "kind": "unavailable",
            "start_at": start, "end_at": start + timedelta(hours=1),
        }).inserted_id
        maintain_series(scope={"tutor_id": users["tutor"]["_id"]}, until=next_day + timedelta(weeks=2))
    blocked = database.lesson_series.find_one({"_id": series["_id"]})
    assert "parent" in blocked["blocked_reason"]
    assert database.lessons.count_documents({"series_id": series["_id"], "start_at": start}) == 0
    assert b"ongoing weekly booking needs attention" in tutor.get("/invoices").data
    database.availability_exceptions.delete_one({"_id": exception_id})
    with app.app_context():
        maintain_series(scope={"tutor_id": users["tutor"]["_id"]}, until=next_day + timedelta(weeks=2))
    assert database.lesson_series.find_one({"_id": series["_id"]})["blocked_reason"] is None
    assert database.lessons.count_documents({"series_id": series["_id"], "start_at": start}) == 1


def test_booking_preview_and_submission_enforce_both_calendars(app, database, users):
    tutor = sign_in(app, users["tutor"])
    form = booking(app, users, repeat="once")
    day = form["starts_at"][:10]
    database.weekly_availability.delete_many({"parent_id": users["parent"]["_id"]})
    with app.app_context():
        weekday = today() + timedelta(days=3)
    database.weekly_availability.insert_one({"parent_id": users["parent"]["_id"], "weekday": weekday.weekday(),
                                             "start_min": 660, "end_min": 780})
    query = {**form, "day": day}
    preview = tutor.get("/lessons/booking-availability", query_string=query).get_json()
    assert preview["tutor"]["available"] == ["00:00–24:00"]
    assert preview["parent"]["available"] == ["11:00–13:00"]
    assert not preview["valid"] and "parent" in preview["reason"]
    assert form["starts_at"] not in dict(preview["slots"])
    assert post(tutor, "/lessons/new", **form).status_code == 409
    form["starts_at"] = day + "T11:00"
    assert tutor.get("/lessons/booking-availability", query_string={**form, "day": day}).get_json()["valid"]
    # Availability can change after the preview, so submission must check again.
    database.weekly_availability.delete_many({"parent_id": users["parent"]["_id"]})
    assert post(tutor, "/lessons/new", **form).status_code == 409
    assert database.lessons.count_documents({}) == 0


def test_topics_have_independent_rates_targets_and_sessions(app, database, users):
    admin = sign_in(app, users["admin"])
    tutor = sign_in(app, users["tutor"])
    for topic, rate in (("Year 7 Mathematics", "70"), ("Chinese", "55")):
        assert post(admin, "/admin/students/tutor", student_id=str(users["student"]["_id"]),
                    tutor_id=str(users["tutor"]["_id"]), topic=topic, rate=rate).status_code == 303
    topics = {row["topic"]: row for row in database.enrollments.find({"topic": {"$exists": True}})}
    math, chinese = topics["Year 7 Mathematics"], topics["Chinese"]
    form = booking(app, users, repeat="once")
    assert post(tutor, "/lessons/new", **form).status_code == 400
    assert post(tutor, "/lessons/new", **form, topic_id=str(math["_id"])).status_code == 303
    other = {**form, "starts_at": form["starts_at"][:10] + "T12:00", "title": "Chinese session"}
    assert post(tutor, "/lessons/new", **other, topic_id=str(chinese["_id"])).status_code == 303
    assert {row["topic"]: row["rate_cents"] for row in database.lessons.find({})} == {
        "Year 7 Mathematics": 7000, "Chinese": 5500,
    }
    assert post(tutor, "/lessons/plan", student_id=form["student_id"], topic_id=str(math["_id"]),
                week=form["starts_at"][:10], target="3").status_code == 303
    assert database.enrollments.find_one({"_id": math["_id"]})["weekly_target"] == 3
    assert database.enrollments.find_one({"_id": chinese["_id"]})["weekly_target"] == 1
    parent_page = sign_in(app, users["parent"]).get("/lessons")
    assert b"Learning topics" in parent_page.data and b"Year 7 Mathematics" in parent_page.data
    wrong_topic = database.enrollments.find_one({"student_id": users["sibling"]["_id"]})
    assert post(tutor, "/lessons/new", **other, topic_id=str(wrong_topic["_id"])).status_code == 400
    assert post(admin, "/admin/students/tutor", student_id=form["student_id"], tutor_id=str(users["tutor"]["_id"]),
                topic_id=str(math["_id"]), topic="Year 7 Mathematics", rate="80").status_code == 303
    assert database.lessons.find_one({"topic_id": math["_id"]})["rate_cents"] == 7000
    later = {**form, "starts_at": form["starts_at"][:10] + "T14:00"}
    assert post(tutor, "/lessons/new", **later, topic_id=str(math["_id"])).status_code == 303
    for lesson in database.lessons.find({}):
        assert post(tutor, f"/lessons/{lesson['_id']}/outcome", status="completed", parent_feedback="Good progress").status_code == 303
    assert post(tutor, "/invoices", parent_id=str(users["parent"]["_id"])).status_code == 303
    invoice = database.invoices.find_one({})
    assert invoice["total_cents"] == 20500
    assert {line["topic"] for line in invoice["lines"]} == {"Year 7 Mathematics", "Chinese"}


def test_admin_can_remove_topic_without_deleting_bookings_or_other_links(app, database, users):
    tutor = sign_in(app, users["tutor"])
    admin = sign_in(app, users["admin"])
    topic = database.enrollments.find_one({"student_id": users["student"]["_id"]})
    sibling_topic = database.enrollments.find_one({"student_id": users["sibling"]["_id"]})
    assert post(tutor, "/lessons/new", **booking(app, users)).status_code == 303
    series = database.lesson_series.find_one({"topic_id": topic["_id"]})
    lesson_ids = {row["_id"] for row in database.lessons.find({"series_id": series["_id"]})}
    path = f"/admin/students/topics/{topic['_id']}/remove"
    assert path.encode() in admin.get("/admin/students").data
    assert post(tutor, path).status_code == 403
    assert post(sign_in(app, users["parent"]), path).status_code == 403
    assert post(admin, path).status_code == 303
    assert database.enrollments.find_one({"_id": topic["_id"]}) is None
    assert database.enrollments.find_one({"_id": sibling_topic["_id"]})
    assert not database.lesson_series.find_one({"_id": series["_id"]})["active"]
    assert {row["_id"] for row in database.lessons.find({"series_id": series["_id"]})} == lesson_ids
    assert post(tutor, "/lessons/new", **booking(app, users, repeat="once", topic_id=str(topic["_id"]))).status_code == 400
    assert post(admin, path).status_code == 404


def test_other_students_bookings_are_unavailable_in_booking_and_change_views(app, database, users):
    tutor = sign_in(app, users["tutor"])
    parent = sign_in(app, users["parent"])
    with app.app_context():
        day = today() + timedelta(days=3)
        original = lesson_record(database, users, "student", boundary(day, 600))
        busy = lesson_record(database, users, "other_student", boundary(day, 780), title="Private other student lesson")
    form = booking(app, users, day=day, repeat="once", starts_at=f"{day}T13:00")
    preview = tutor.get("/lessons/booking-availability", query_string={**form, "day": str(day)}).get_json()
    assert "13:00–14:00" in preview["tutor"]["unavailable"]
    assert "11:00–13:00" in preview["tutor"]["available"]
    assert not preview["valid"]
    assert f"{day}T13:00" not in dict(preview["slots"])
    assert f"{day}T12:15" not in dict(preview["slots"])
    assert f"{day}T14:00" in dict(preview["slots"])
    assert post(tutor, "/lessons/new", **form).status_code == 409
    path = f"/lessons/{original['_id']}/change"
    for client in (tutor, parent):
        page = client.get(path, query_string={"date": str(day)})
        assert page.status_code == 200
        assert b"13:00\xe2\x80\x9314:00 \xc2\xb7 Booked \xc2\xb7 unavailable" in page.data
        assert f'value="{day}T13:00"'.encode() not in page.data
        assert f'value="{day}T12:15"'.encode() not in page.data
        assert f'value="{day}T14:00"'.encode() in page.data
        assert busy["title"].encode() not in page.data
        assert post(client, path, starts_at=f"{day}T13:00").status_code == 409
    assert database.change_requests.count_documents({}) == 0


@pytest.mark.parametrize("app", [{"APP_TIMEZONE": "Australia/Sydney"}], indirect=True)
def test_ongoing_booking_preserves_local_time_across_daylight_saving(app, database, users):
    tutor = sign_in(app, users["tutor"])
    form = booking(app, users, day=date(2030, 9, 29))
    assert post(tutor, "/lessons/new", **form).status_code == 303
    records = list(database.lessons.find({}).sort("start_at", 1))
    with app.app_context():
        assert {stamp(record["start_at"], "time") for record in records} == {"10:00"}
    assert records[1]["start_at"] - records[0]["start_at"] == timedelta(hours=167)


def test_legacy_enrollment_index_upgrade_preserves_links_and_rates(app, database, users):
    lesson = lesson_record(database, users, "student", now() + timedelta(days=2), rate_cents=6200)
    database.enrollments.drop_index("tutor_id_1_student_id_1_topic_key_1")
    database.enrollments.create_index([("tutor_id", 1), ("student_id", 1)], unique=True)
    with app.app_context():
        ensure_indexes()
        ensure_indexes()
    indexes = database.enrollments.index_information()
    assert "tutor_id_1_student_id_1" not in indexes
    assert "tutor_id_1_student_id_1_topic_key_1" in indexes
    assert database.enrollments.count_documents({"topic": "General lessons", "rate_cents": 6500}) == 3
    enrollment = database.enrollments.find_one({"student_id": users["student"]["_id"]})
    migrated = database.lessons.find_one({"_id": lesson["_id"]})
    assert migrated["topic_id"] == enrollment["_id"] and migrated["topic"] == "General lessons"
    assert migrated["rate_cents"] == 6200


def test_only_relevant_outcome_fields_are_validated(app, database, users):
    tutor = sign_in(app, users["tutor"])
    upcoming = lesson_record(database, users, "student", now() + timedelta(days=3))
    path = f"/lessons/{upcoming['_id']}/outcome"
    assert post(tutor, path, status="cancelled", reason="Family plans", parent_feedback="x" * 5000).status_code == 303
    saved = database.lessons.find_one({"_id": upcoming["_id"]})
    assert saved["cancellation_reason"] == "Family plans"
    assert saved["parent_feedback"] == saved["student_feedback"] == ""
    assert post(tutor, path, status="completed", parent_feedback="Good progress", reason="x" * 2000,
                charge_late="yes").status_code == 303
    saved = database.lessons.find_one({"_id": upcoming["_id"]})
    assert saved["cancellation_reason"] == "" and saved["charge"]
    past = lesson_record(database, users, "student", now() - timedelta(hours=3))
    assert post(tutor, f"/lessons/{past['_id']}/outcome", status="did_not_attend").status_code == 303


def test_setup_reminders_follow_role_and_disappear_when_complete(app, database, users):
    tutor = sign_in(app, users["tutor"])
    database.users.update_one({"_id": users["tutor"]["_id"]}, {"$set": {"billing": {"name": "Incomplete"}}})
    database.weekly_availability.delete_many({"tutor_id": users["tutor"]["_id"]})
    page = tutor.get("/homework")
    assert b"weekly availability is missing" in page.data and b"billing profile is incomplete" in page.data
    assert b"billing profile is incomplete" not in sign_in(app, users["parent"]).get("/homework").data
    assert post(tutor, "/profile", **users["tutor"]["billing"]).status_code == 303
    assert post(tutor, "/availability/weekly", weekday="0", start="09:00", end="17:00").status_code == 303
    page = tutor.get("/homework")
    assert b"weekly availability is missing" not in page.data and b"billing profile is incomplete" not in page.data
