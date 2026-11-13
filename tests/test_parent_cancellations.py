from datetime import timedelta

import pytest

from portal.billing import generate
from portal.core import Problem, now
from test_portal import app, database, users, sign_in, post, lesson_record


@pytest.mark.parametrize("hours,late", [(48, False), (24, False), (23, True)])
def test_parent_cancels_immediately_with_terms_and_tutor_review(app, database, users, monkeypatch, hours, late):
    timestamp = now().replace(microsecond=0)
    monkeypatch.setattr("portal.lessons.now", lambda: timestamp)
    lesson = lesson_record(database, users, "student", timestamp + timedelta(hours=hours))
    path = f"/lessons/{lesson['_id']}"
    parent = sign_in(app, users["parent"])
    page = parent.get(path + "/cancel")
    assert page.status_code == 200 and b"We value consistency" in page.data
    assert (b"As per our contract" in page.data) == late
    assert post(parent, path + "/cancel").status_code == 400
    assert sign_in(app, users["other_parent"]).get(path + "/cancel").status_code == 404
    assert post(sign_in(app, users["student"]), path + "/cancel", confirm="yes").status_code == 403
    if late:
        assert post(parent, path + "/cancel", confirm="yes").status_code == 409
    assert post(parent, path + "/cancel", confirm="yes", acknowledge_late="yes", reason="Family commitment").status_code == 303
    saved = database.lessons.find_one({"_id": lesson["_id"]})
    assert saved["status"] == ("cancelled_late" if late else "cancelled")
    assert saved["cancellation_reason"] == "Family commitment"
    assert saved["cancellation_fee_pending"] and not saved["charge"]
    assert saved["cancelled_at"] == timestamp
    tutor = sign_in(app, users["tutor"])
    assert b"Review cancellation" in tutor.get("/lessons").data
    with app.app_context():
        with pytest.raises(Problem, match="no finalized"):
            generate(users["tutor"]["_id"], users["parent"]["_id"])
    if not late:
        assert post(tutor, path + "/cancellation-fee", decision="charge").status_code == 400
    # Reviewing days later uses the recorded cancellation time.
    monkeypatch.setattr("portal.lessons.now", lambda: timestamp + timedelta(days=4))
    assert post(tutor, path + "/cancellation-fee", decision="charge" if late else "waive").status_code == 303
    saved = database.lessons.find_one({"_id": lesson["_id"]})
    assert not saved["cancellation_fee_pending"] and saved["charge"] == late
    assert post(tutor, path + "/cancellation-fee", decision="waive").status_code == 409
    assert post(parent, path + "/cancel", confirm="yes", acknowledge_late="yes").status_code == 409


def test_late_terms_rechecked_after_confirmation_page(app, database, users, monkeypatch):
    timestamp = now().replace(microsecond=0)
    monkeypatch.setattr("portal.lessons.now", lambda: timestamp)
    lesson = lesson_record(database, users, "student", timestamp + timedelta(hours=25))
    parent = sign_in(app, users["parent"])
    path = f"/lessons/{lesson['_id']}/cancel"
    assert b"As per our contract" not in parent.get(path).data
    monkeypatch.setattr("portal.lessons.now", lambda: timestamp + timedelta(hours=2))
    assert post(parent, path, confirm="yes").status_code == 409
    assert database.lessons.find_one({"_id": lesson["_id"]})["status"] == "scheduled"
