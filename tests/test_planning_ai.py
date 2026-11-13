from copy import deepcopy
from datetime import UTC, datetime, timedelta
from io import BytesIO
import json

from bson import ObjectId
from flask import Flask
from PIL import Image
import pytest
import requests

from portal import ai, planning
from portal.core import Problem
from test_portal import app, database, users, csrf, lesson_record, post, sign_in


def valid_plan(**extra):
    return {"version": 1, "title": "Algebra", "body": "## Equations\nPractice solving equations.", "goals": [
        {"id": "a" * 24, "title": "Solve linear equations", "due_at": "2026-12-01", "completed": False},
        {"id": "b" * 24, "title": "Factorise quadratics", "due_at": "2026-12-08", "completed": False},
    ], **extra}


def test_json_plan_roundtrip_with_date_only_goals():
    plan = planning.validate_plan(valid_plan())
    assert planning.export_plan(plan) == valid_plan()
    assert plan["goals"][0]["due_at"].tzinfo is not None


def test_legacy_timestamp_goals_export_as_dates_in_portal_timezone():
    app = Flask(__name__)
    from zoneinfo import ZoneInfo
    app.extensions["zone"] = ZoneInfo("Asia/Shanghai")
    with app.app_context():
        payload = valid_plan(goals=[{"title": "Legacy goal", "due_at": "2026-11-30T20:00:00Z", "completed": False}])
        plan = planning.validate_plan(payload)
        assert planning.export_plan(plan)["goals"][0]["due_at"] == "2026-12-01"
        assert planning.timeline(plan, datetime(2026, 11, 30, 16, tzinfo=UTC))[0]["due_date"] == "2026-12-01"


@pytest.mark.parametrize("payload", [None, [], {"version": True}, valid_plan(title=""), valid_plan(body="x" * 32001),
    valid_plan(goals=[{"title": "x", "due_at": "2026-11-20T12:00:00", "completed": False}]),
    valid_plan(goals=[{"title": "x", "due_at": "2026-02-30", "completed": False}]),
    valid_plan(goals=[{"title": "x", "due_at": "2200-01-01", "completed": False}]),
    valid_plan(goals=[{"title": "x", "due_at": "2026-11-20T12:00:00Z", "completed": "yes"}]),
    valid_plan(goals=[{"title": "x", "due_at": "2026-11-20T12:00:00Z", "completed": False, "id": "$bad"}]),
    valid_plan(goals=[valid_plan()["goals"][0]] * 2), valid_plan(unexpected="ignored?"),
])
def test_json_plan_rejects_invalid_shape(payload):
    with pytest.raises(Problem):
        planning.validate_plan(payload)


def test_timeline_highlights_current_pending_goal_and_preserves_completed_state():
    plan = planning.validate_plan(valid_plan())
    rows = planning.timeline(plan, datetime(2026, 12, 4, tzinfo=UTC))
    assert [row["state"] for row in rows] == ["current", "future"]
    plan["goals"][0]["completed"] = True
    rows = planning.timeline(plan, datetime(2026, 12, 4, tzinfo=UTC))
    assert [row["state"] for row in rows] == ["completed", "current"]


def test_board_render_is_bounded_and_contains_negative_direction_strokes():
    rows = [{"type": "text", "text": "x + 2 = 4", "x": 0, "y": 0, "w": 200, "h": 70, "font": 20},
            {"type": "path", "x": 10, "y": 80, "w": 100, "h": 50, "points": [[0, 0], [100, 50]], "color": "#ff0000"},
            {"type": "line", "x": 500, "y": 100, "w": 40, "h": 40, "dx": -40, "dy": -40}]
    import base64
    result = ai.render_board(rows, ObjectId())
    with Image.open(BytesIO(base64.b64decode(result.split(",", 1)[1]))) as image:
        assert image.width <= 1400 and image.height <= 1000


def test_course_plan_auth_markdown_upload_json_and_revision(app, database, users):
    topic = database.enrollments.find_one({"student_id": users["student"]["_id"]})
    path = f"/course-plans/{topic['_id']}"
    tutor, parent, student = (sign_in(app, users[key]) for key in ("tutor", "parent", "student"))
    payload = valid_plan()
    response = tutor.post(path, data={"csrf_token": csrf(tutor), "title": payload["title"], "body": payload["body"], "goals": json.dumps(payload["goals"]), "revision": "0", "files": (BytesIO(b"notes"), "notes.txt")})
    assert response.status_code == 303
    saved = database.course_plans.find_one({"_id": topic["_id"]})
    assert saved["revision"] == 1
    assert b"<h2>Equations</h2>" in parent.get(path).data
    assert tutor.get(path + "/download").json == payload
    tutor_page = tutor.get(path).data
    assert b"Download JSON" in tutor_page and b"data-schema-dialog" in tutor_page
    assert b"Delete course plan" in tutor_page and b"Are you sure" in tutor_page
    assert b"Set a specific date and time" not in tutor_page
    for client in (parent, student):
        assert client.get(path + "/download").status_code == 403
        page = client.get(path).data
        assert b"Download JSON" not in page and b"data-schema-dialog" not in page
        assert b"Delete course plan" not in page
    file = database.course_plan_files.find_one({"topic_id": topic["_id"]})
    download = path + f"/files/{file['_id']}"
    assert parent.get(download).data == b"notes"
    assert student.get(download).status_code == 200
    for key in ("other_parent", "other_tutor", "other_student"):
        other = sign_in(app, users[key])
        assert other.get(path).status_code == 404
        assert other.get(download).status_code == 404
    assert post(parent, path, **{"title": "Hacked"}).status_code == 403
    assert post(tutor, path, title="Outdated", body="", goals="[]", revision="0").status_code == 409
    assert post(tutor, path + f"/goals/{'a' * 24}", completed="yes").status_code == 303
    checked = database.course_plans.find_one({"_id": topic["_id"]})
    assert checked["goals"][0]["completed"] and checked["revision"] == 2


def test_delete_course_plan_requires_confirmation_revision_and_topic_access(app, database, users):
    topic = database.enrollments.find_one({"student_id": users["student"]["_id"]})
    topic_id = topic["_id"]
    path = f"/course-plans/{topic_id}"
    database.course_plans.insert_one({"_id": topic_id, "revision": 1, **planning.validate_plan(valid_plan())})
    lesson = lesson_record(database, users, "student", datetime.now(UTC), topic_id=topic_id)
    database.course_plan_files.insert_one({"topic_id": topic_id, "filename": "notes.txt", "data": b"notes"})
    comment_id = ObjectId()
    database.comments.insert_one({"_id": comment_id, "kind": "course", "target_id": topic_id})
    database.comment_images.insert_one({"comment_id": comment_id, "data": b"image"})
    database.notifications.insert_one({"kind": "course", "target_id": topic_id, "comment_id": comment_id})
    other_comment = ObjectId()
    database.comments.insert_one({"_id": other_comment, "kind": "lesson", "target_id": lesson["_id"]})
    for role in ("parent", "student", "other_tutor"):
        client = sign_in(app, users[role])
        assert post(client, path + "/delete", confirmed="yes", revision="1").status_code == (404 if role == "other_tutor" else 403)
    tutor = sign_in(app, users["tutor"])
    assert post(tutor, path + "/delete", revision="1").status_code == 400
    assert post(tutor, path + "/delete", confirmed="yes", revision="0").status_code == 409
    assert database.course_plans.count_documents({"_id": topic_id}) == 1
    assert tutor.post(path + "/delete", data={"confirmed": "yes", "revision": "1"}).status_code == 400
    response = post(tutor, path + "/delete", confirmed="yes", revision="1")
    assert response.status_code == 303 and response.location.endswith("/course-plans")
    assert database.course_plans.count_documents({"_id": topic_id}) == 0
    assert database.course_plan_files.count_documents({"topic_id": topic_id}) == 0
    assert database.comments.count_documents({"kind": "course", "target_id": topic_id}) == 0
    assert database.comment_images.count_documents({"comment_id": comment_id}) == 0
    assert database.notifications.count_documents({"kind": "course", "target_id": topic_id}) == 0
    assert database.enrollments.count_documents({"_id": topic_id}) == 1
    assert database.lessons.count_documents({"_id": lesson["_id"]}) == 1
    assert database.comments.count_documents({"_id": other_comment}) == 1


def test_course_import_validates_before_replacing_content(app, database, users):
    topic = database.enrollments.find_one({"student_id": users["student"]["_id"]})
    path = f"/course-plans/{topic['_id']}/import"
    tutor = sign_in(app, users["tutor"])
    for data in (b'{"version":1}', b"[", b"x" * (256 * 1024 + 1)):
        response = tutor.post(path, data={"csrf_token": csrf(tutor), "plan_file": (BytesIO(data), "plan.json")})
        assert response.status_code in {400, 413}
        assert database.course_plans.count_documents({}) == 0
    response = tutor.post(path, data={"csrf_token": csrf(tutor), "revision": "0", "plan_file": (BytesIO(json.dumps(valid_plan()).encode()), "plan.json")})
    assert response.status_code == 303
    assert database.course_plans.find_one({})["title"] == "Algebra"


def test_lesson_plan_optional_and_readonly_for_family_and_historical_topic(app, database, users):
    lesson = lesson_record(database, users, "student", datetime.now(UTC) + timedelta(days=1))
    path = f"/lessons/{lesson['_id']}"
    tutor = sign_in(app, users["tutor"])
    assert post(tutor, path + "/plan", lesson_plan="## Revision\nSolve equations", plan_revision="0").status_code == 303
    parent = sign_in(app, users["parent"])
    assert b"<h2>Revision</h2>" in parent.get(path).data
    assert post(parent, path + "/plan", lesson_plan="No").status_code == 403
    assert post(tutor, path + "/plan", lesson_plan="stale", plan_revision="0").status_code == 409
    assert post(tutor, path + "/plan", lesson_plan="", plan_revision="1").status_code == 303
    assert database.lessons.find_one({"_id": lesson["_id"]})["lesson_plan"] == ""
    database.enrollments.delete_many({"student_id": lesson["student_id"]})
    assert tutor.get(path).status_code == 200


def install_course(database, users):
    topic = database.enrollments.find_one({"student_id": users["student"]["_id"]})
    database.course_plans.insert_one({"_id": topic["_id"], "revision": 1, **planning.validate_plan(valid_plan())})
    lesson = lesson_record(database, users, "student", datetime.now(UTC), topic_id=topic["_id"], lesson_plan="Practice equations")
    return lesson


def test_ai_drafts_are_tutor_only_grounded_and_not_saved(app, database, users, monkeypatch):
    app.config["OPENROUTER_API_KEY"] = "secret-test-key"
    lesson = install_course(database, users)
    database.whiteboards.insert_one({"_id": lesson["_id"], "elements": {"working": {"type": "text", "text": "2x = 6", "x": 10, "y": 10, "w": 150, "h": 70}}})
    calls = []
    def response(url, **kwargs):
        calls.append((url, kwargs))
        payload = {"lesson_plan": "## Warmup\n5 min equations"} if "flash-lite" in kwargs["json"]["model"] else {"parent_feedback": "Worked on equations.", "student_feedback": "Keep practising equations!"}
        result = requests.Response(); result.status_code = 200
        result._content = json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}).encode()
        return result
    monkeypatch.setattr(requests, "post", response)
    tutor = sign_in(app, users["tutor"])
    path = f"/lessons/{lesson['_id']}"
    headers = {"X-CSRFToken": csrf(tutor)}
    plan = tutor.post(path + "/generate-plan", headers=headers, json={})
    assert plan.json["lesson_plan"].startswith("## Warmup")
    assert database.lessons.find_one({"_id": lesson["_id"]})["lesson_plan"] == "Practice equations"
    feedback = tutor.post(path + "/generate-feedback", headers=headers, json={"status": "completed"})
    assert feedback.json["parent_feedback"] == "Worked on equations."
    assert "parent_feedback" not in database.lessons.find_one({"_id": lesson["_id"]})
    body = calls[1][1]["json"]["messages"][1]["content"]
    assert "2x = 6" in body[0]["text"] and "Practice equations" in body[0]["text"] and "Algebra" in body[0]["text"]
    assert body[1]["type"] == "image_url"
    assert "secret-test-key" not in json.dumps(calls[1][1]["json"])
    assert tutor.post(path + "/generate-feedback", headers=headers, json={"status": "completed"}).status_code == 429
    for role in ("parent", "student", "other_tutor"):
        client = sign_in(app, users[role])
        result = client.post(path + "/generate-plan", headers={"X-CSRFToken": csrf(client)}, json={})
        assert result.status_code == (404 if role == "other_tutor" else 403)


def test_ai_config_errors_and_failed_provider_do_not_modify_lesson(app, database, users, monkeypatch):
    app.config["OPENROUTER_API_KEY"] = ""
    lesson = install_course(database, users)
    tutor = sign_in(app, users["tutor"])
    path = f"/lessons/{lesson['_id']}"
    headers = {"X-CSRFToken": csrf(tutor)}
    response = tutor.post(path + "/generate-plan", json={}, headers=headers)
    assert response.status_code == 503 and response.is_json
    app.config["OPENROUTER_API_KEY"] = "configured"
    def unavailable(*args, **kwargs):
        raise requests.Timeout("secret internal details")
    monkeypatch.setattr(requests, "post", unavailable)
    response = tutor.post(path + "/generate-plan", json={}, headers=headers)
    assert response.status_code == 502 and b"secret internal details" not in response.data
    assert database.lessons.find_one({"_id": lesson["_id"]})["lesson_plan"] == "Practice equations"
    assert tutor.post(path + "/generate-feedback", json={"status": "scheduled"}, headers=headers).status_code == 400
