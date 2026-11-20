from datetime import UTC, date, datetime, timedelta

import pytest

from portal.recurring import HORIZON_WEEKS, maintain_series
from test_portal import app, database, users, post, sign_in
from test_time_preferences import set_preferences


@pytest.mark.parametrize("target", ["tutor", "parent", "student"])
def test_admin_can_add_edit_remove_target_availability(app, database, users, target):
    admin = sign_in(app, users["admin"])
    set_preferences(database, users["admin"], "Australia/Melbourne")
    target_id = str(users[target]["_id"])
    query = {"account_id": target_id}
    page = admin.get("/availability", query_string=query)
    assert page.status_code == 200 and b"Manage availability for" in page.data
    assert users[target]["name"].encode() in page.data
    # Explicit student targets and parent family targets preserve their scopes.
    assert post(admin, "/availability/weekly", **query, weekday="1", start="15:15", end="17:45",
                week="2030-01-15").status_code == 303
    owner = {"tutor_id" if target == "tutor" else "parent_id": users["tutor" if target == "tutor" else "parent"]["_id"]}
    if target == "student":
        owner["student_id"] = users["student"]["_id"]
    block = database.weekly_availability.find_one({**owner, "start_min": 915})
    assert block["start_at"] == datetime(2030, 1, 15, 4, 15, tzinfo=UTC)
    assert post(admin, "/availability/weekly", **query, entry_id=str(block["_id"]), weekday="1",
                start="16:00", end="18:00", week="2030-01-15").status_code == 303
    assert database.weekly_availability.find_one({"_id": block["_id"]})["start_min"] == 960
    assert post(admin, f"/availability/weekly/{block['_id']}/remove", **query).status_code == 303
    assert database.weekly_availability.find_one({"_id": block["_id"]}) is None
    assert post(admin, "/availability/exception", **query, kind="unavailable", starts_at="2030-01-15T16:00",
                ends_at="2030-01-15T17:00").status_code == 303
    exception = database.availability_exceptions.find_one(owner)
    assert exception["start_at"] == datetime(2030, 1, 15, 5, tzinfo=UTC)
    assert post(admin, "/availability/exception", **query, entry_id=str(exception["_id"]), kind="preferred",
                starts_at="2030-01-15T17:00", ends_at="2030-01-15T18:00").status_code == 303
    response = post(admin, f"/availability/exception/{exception['_id']}/remove", **query)
    assert response.status_code == 303 and f"account_id={target_id}" in response.location


def test_admin_student_customization_preserves_inherited_sibling_hours_and_entry_ownership(app, database, users):
    admin = sign_in(app, users["admin"])
    inherited = database.weekly_availability.find_one({"parent_id": users["parent"]["_id"]})
    target = {"account_id": str(users["student"]["_id"])}
    assert post(admin, "/availability/weekly", **target, entry_id=str(inherited["_id"]), weekday="0",
                start="09:00", end="10:00").status_code == 303
    assert database.weekly_availability.find_one({"_id": inherited["_id"]})["start_min"] == 0
    assert database.users.find_one({"_id": users["student"]["_id"]})["availability_parent_id"] == users["parent"]["_id"]
    assert "availability_parent_id" not in database.users.find_one({"_id": users["sibling"]["_id"]})
    own = database.weekly_availability.find_one({"student_id": users["student"]["_id"]})
    assert post(admin, f"/availability/weekly/{own['_id']}/remove",
                account_id=str(users["other_student"]["_id"])).status_code == 404


@pytest.mark.parametrize("role", ["tutor", "parent", "student"])
def test_nonadmins_cannot_edit_other_accounts_availability(app, database, users, role):
    client = sign_in(app, users[role])
    target = {"account_id": str(users["other_tutor"]["_id"])}
    assert client.get("/availability", query_string=target).status_code in {403, 404}
    assert post(client, "/availability/weekly", **target, weekday="0", start="09:00", end="10:00").status_code in {403, 404}
    assert database.weekly_availability.count_documents({"tutor_id": users["other_tutor"]["_id"]}) == 0


def admin_booking(users, **extra):
    return {"tutor_id": str(users["tutor"]["_id"]), "student_id": str(users["student"]["_id"]),
            "title": "Admin lesson", "starts_at": "2030-01-15T15:00", "minutes": "60", "repeat": "once", **extra}


@pytest.mark.parametrize("repeat", ["once", "count", "forever"])
def test_admin_books_without_availability_and_override_persists_for_future_weeks(app, database, users, repeat):
    database.weekly_availability.delete_many({})
    set_preferences(database, users["admin"], "Australia/Melbourne")
    admin = sign_in(app, users["admin"])
    assert b"Admin bookings can use any time" in admin.get("/lessons/new").data
    form = admin_booking(users, repeat=repeat, weeks="3")
    preview = admin.get("/lessons/booking-availability", query_string={**form, "day": "2030-01-15"}).json
    assert preview["valid"] and preview["tutor"]["available"] == []
    assert post(admin, "/lessons/new", **form).status_code == 303
    expected = {"once": 1, "count": 3, "forever": HORIZON_WEEKS}[repeat]
    assert database.lessons.count_documents({}) == expected
    first = database.lessons.find_one({}, sort=[("start_at", 1)])
    assert first["start_at"] == datetime(2030, 1, 15, 4, tzinfo=UTC)
    assert first["availability_override"] and first["created_by"] == users["admin"]["_id"]
    assert admin.get(f"/lessons/{first['_id']}").status_code == 200
    assert b"Admin lesson" in sign_in(app, users["tutor"]).get("/lessons?week=2030-01-15").data
    if repeat == "forever":
        with app.app_context():
            maintain_series(scope={"tutor_id": users["tutor"]["_id"]}, until=date(2030, 7, 1))
        assert database.lessons.count_documents({}) > HORIZON_WEEKS
        assert all(row["availability_override"] for row in database.lessons.find({}))


def test_admin_override_still_rejects_conflicts_bad_topics_past_times_and_inactive_accounts(app, database, users):
    database.weekly_availability.delete_many({})
    admin = sign_in(app, users["admin"])
    form = admin_booking(users)
    assert post(admin, "/lessons/new", **form).status_code == 303
    for changes in ({}, {"student_id": str(users["sibling"]["_id"])}, {"repeat": "forever"}):
        assert post(admin, "/lessons/new", **{**form, **changes}).status_code == 409
    assert post(admin, "/lessons/new", **{**form, "starts_at": "2000-01-01T10:00"}).status_code == 400
    wrong_topic = database.enrollments.find_one({"student_id": users["sibling"]["_id"]})
    assert post(admin, "/lessons/new", **{**form, "starts_at": "2030-01-15T17:00", "topic_id": str(wrong_topic["_id"])}).status_code == 400
    database.users.update_one({"_id": users["tutor"]["_id"]}, {"$set": {"active": False}})
    assert post(admin, "/lessons/new", **{**form, "starts_at": "2030-01-15T18:00"}).status_code == 409
    assert database.lessons.count_documents({}) == 1


def test_tutor_cannot_request_admin_availability_override(app, database, users):
    database.weekly_availability.delete_many({})
    tutor = sign_in(app, users["tutor"])
    assert post(tutor, "/lessons/new", **admin_booking(users), skip_availability="yes",
                availability_override="yes").status_code == 409
    assert database.lessons.count_documents({}) == 0
