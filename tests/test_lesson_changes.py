from datetime import UTC, datetime, timedelta

import pytest

from portal.core import now
from test_portal import app, database, users, sign_in, post, lesson_record


@pytest.mark.parametrize("timestamp", [
    datetime(2026, 11, 13, 1, 0, tzinfo=UTC),
    datetime(2026, 11, 16, 2, 15, tzinfo=UTC),
    datetime(2026, 11, 16, 2, 45, tzinfo=UTC),
    datetime(2026, 11, 16, 3, 0, tzinfo=UTC),
], ids=["before-start", "during-lesson", "at-end", "after-end"])
def test_tutor_can_record_completion_and_feedback_regardless_of_scheduled_end(
    app, database, users, monkeypatch, timestamp,
):
    monkeypatch.setattr("portal.lessons.now", lambda: timestamp)
    lesson = lesson_record(database, users, "student", datetime(2026, 11, 16, 1, 45, tzinfo=UTC))
    tutor = sign_in(app, users["tutor"])
    path = f"/lessons/{lesson['_id']}"

    response = post(tutor, path + "/outcome", status="completed",
                    parent_feedback="Completed the lesson early; good progress.",
                    student_feedback="Keep practising the examples.")

    assert response.status_code == 303
    saved = database.lessons.find_one({"_id": lesson["_id"]})
    assert saved["status"] == "completed" and saved["charge"]
    assert saved["parent_feedback"] == "Completed the lesson early; good progress."
    assert saved["student_feedback"] == "Keep practising the examples."
    assert saved["billing_parent_id"] == users["parent"]["_id"]
    assert saved["finalized_at"] == timestamp
    assert b"Lesson outcome and feedback saved." in tutor.get(response.location).data


def test_nonattendance_still_requires_the_scheduled_lesson_to_end(app, database, users):
    lesson = lesson_record(database, users, "student", now() + timedelta(days=3))
    tutor = sign_in(app, users["tutor"])

    response = post(tutor, f"/lessons/{lesson['_id']}/outcome", status="did_not_attend",
                    parent_feedback="Nonattendance recorded too early.")

    assert response.status_code == 400
    assert "Mark the outcome once the lesson’s scheduled time has ended." in response.get_data(as_text=True)
    saved = database.lessons.find_one({"_id": lesson["_id"]})
    assert saved["status"] == "scheduled" and "charge" not in saved


def test_feedback_is_required_for_parent_optional_for_student_and_audience_specific(app, database, users):
    tutor = sign_in(app, users["tutor"])
    parent = sign_in(app, users["parent"])
    student = sign_in(app, users["student"])
    lesson = lesson_record(database, users, "student", now() - timedelta(hours=3))
    path = f"/lessons/{lesson['_id']}"
    assert post(tutor, path + "/outcome", status="completed", student_feedback="Student note only").status_code == 400
    assert post(tutor, path + "/outcome", status="completed", feedback="Old student-directed form").status_code == 400
    assert post(tutor, path + "/outcome", status="completed", parent_feedback="Parent progress report").status_code == 303
    saved = database.lessons.find_one({"_id": lesson["_id"]})
    assert saved["student_feedback"] == "" and saved["parent_feedback"] == "Parent progress report"
    assert post(tutor, path + "/outcome", status="completed", parent_feedback="Parent progress report",
                student_feedback="Student practice advice").status_code == 303
    parent_page = parent.get(path)
    student_page = student.get(path)
    assert b"Parent progress report" in parent_page.data and b"Student practice advice" not in parent_page.data
    assert b"Student practice advice" in student_page.data and b"Parent progress report" not in student_page.data
    assert b"Parent progress report" in tutor.get(path).data
    assert b"Student practice advice" in tutor.get(path).data
    assert sign_in(app, users["other_parent"]).get(path).status_code == 404
    assert lesson["title"].encode() in parent.get("/lessons?view=past").data
    assert post(parent, path + "/outcome", status="completed", parent_feedback="Forbidden").status_code == 403


@pytest.mark.parametrize("hours,expected", [(25, 400), (24, 400), (23, 303), (-1, 400)])
def test_cancellation_charge_enforces_actual_notice_window(app, database, users, monkeypatch, hours, expected):
    timestamp = now().replace(microsecond=0)
    monkeypatch.setattr("portal.lessons.now", lambda: timestamp)
    lesson = lesson_record(database, users, "student", timestamp + timedelta(hours=hours))
    tutor = sign_in(app, users["tutor"])
    path = f"/lessons/{lesson['_id']}"
    page = tutor.get(path)
    assert (b'name="charge_late"' in page.data) == (0 <= hours < 24)
    response = post(tutor, path + "/outcome", status="cancelled_late", charge_late="yes", parent_feedback="Cancellation recorded")
    assert response.status_code == expected
    saved = database.lessons.find_one({"_id": lesson["_id"]})
    if expected == 303:
        assert saved["charge"] and saved["status"] == "cancelled_late"
    else:
        assert saved["status"] == "scheduled" and "charge" not in saved
        assert post(tutor, path + "/outcome", status="cancelled_late", parent_feedback="Cancellation recorded").status_code == 303
        saved = database.lessons.find_one({"_id": lesson["_id"]})
        assert not saved["charge"] and saved["status"] == "cancelled"


def test_editing_cancellation_does_not_turn_early_notice_into_charge(app, database, users, monkeypatch):
    timestamp = now().replace(microsecond=0)
    monkeypatch.setattr("portal.lessons.now", lambda: timestamp)
    lesson = lesson_record(database, users, "student", timestamp + timedelta(hours=25))
    tutor = sign_in(app, users["tutor"])
    path = f"/lessons/{lesson['_id']}"
    assert post(tutor, path + "/outcome", status="cancelled", parent_feedback="Early cancellation").status_code == 303
    monkeypatch.setattr("portal.lessons.now", lambda: timestamp + timedelta(hours=2))
    assert b'name="charge_late"' not in tutor.get(path).data
    assert post(tutor, path + "/outcome", status="cancelled_late", charge_late="yes",
                parent_feedback="Changed decision").status_code == 400
    saved = database.lessons.find_one({"_id": lesson["_id"]})
    assert saved["cancelled_at"] == timestamp and not saved["charge"]
