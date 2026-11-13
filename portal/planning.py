"""Student/topic course plans and optional lesson agendas."""
from datetime import UTC, date, datetime, time
from io import BytesIO
import json
import re

from bson import ObjectId
from pymongo import ReturnDocument
from flask import Blueprint, abort, current_app, flash, g, has_app_context, redirect, render_template, request, send_file, url_for

from .core import Problem, atomic, db, lock_accounts, now, object_id, people, requires, text_field
from .lessons import get_lesson
from .uploads import FILE_ACCEPT, selected_uploads

bp = Blueprint("planning", __name__)
MAX_BODY = 32000
MAX_GOALS = 100
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Lesson Desk course plan", "type": "object", "additionalProperties": False,
    "required": ["version", "title", "body", "goals"],
    "properties": {
        "version": {"const": 1}, "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "body": {"type": "string", "maxLength": MAX_BODY, "description": "Markdown"},
        "goals": {"type": "array", "maxItems": MAX_GOALS, "items": {
            "type": "object", "additionalProperties": False, "required": ["title", "due_at", "completed"],
            "properties": {
                "id": {"type": "string", "pattern": "^[a-f0-9]{24}$"},
                "title": {"type": "string", "minLength": 1, "maxLength": 300},
                "due_at": {"type": "string", "format": "date", "description": "Goal date in YYYY-MM-DD format; no time is needed"},
                "completed": {"type": "boolean"},
            },
        }},
    },
}


def topic_scope():
    role = g.user["role"]
    if role == "parent":
        students = db().users.find({"role": "student", "active": True, "parent_id": g.user["_id"]}, {"_id": 1})
        return {"student_id": {"$in": [row["_id"] for row in students]}}
    return {f"{role}_id": g.user["_id"]}


def get_topic(topic_id, transaction=None):
    topic = db().enrollments.find_one({"_id": object_id(topic_id), **topic_scope()}, session=transaction)
    if not topic:
        abort(404)
    student = db().users.find_one({"_id": topic["student_id"], "active": True, "role": "student"}, {"_id": 1}, session=transaction)
    if not student:
        abort(404)
    return topic


def get_plan(topic_id):
    topic = get_topic(topic_id)
    return db().course_plans.find_one({"_id": topic["_id"]}) or {
        "_id": topic["_id"], "title": topic.get("topic", "General lessons"), "body": "", "goals": [], "revision": 0,
    }


def goal_date(goal):
    due = goal["due_at"]
    # Existing plans keep their stored timestamps; present their calendar date in
    # the same timezone used by the rest of the portal.
    if has_app_context():
        due = due.astimezone(current_app.extensions["zone"])
    return due.date()


def serialize_goal(goal):
    return {"id": goal["id"], "title": goal["title"], "due_at": goal_date(goal).isoformat(), "completed": goal["completed"]}


def export_plan(plan):
    return {"version": 1, "title": plan["title"], "body": plan["body"], "goals": [serialize_goal(goal) for goal in plan.get("goals", [])]}


def validate_plan(payload):
    if not isinstance(payload, dict) or set(payload) - {"version", "title", "body", "goals"}:
        raise Problem("Use the course plan JSON schema shown on this page.")
    if type(payload.get("version")) is not int or payload["version"] != 1:
        raise Problem("Choose a version 1 course plan JSON file.")
    for key, maximum in (("title", 200), ("body", MAX_BODY)):
        if not isinstance(payload.get(key), str) or len(payload[key]) > maximum or "\x00" in payload[key] or (key == "title" and not payload[key].strip()):
            raise Problem(f"Enter a valid {key} (up to {maximum} characters).")
    rows = payload.get("goals")
    if not isinstance(rows, list) or len(rows) > MAX_GOALS:
        raise Problem("Add up to 100 timeline goals.")
    goals, ids = [], set()
    for goal in rows:
        if not isinstance(goal, dict) or set(goal) - {"id", "title", "due_at", "completed"}:
            raise Problem("Each timeline goal needs a title, due_at date, and completed checkbox.")
        title = goal.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 300 or "\x00" in title:
            raise Problem("Give each timeline goal a title of up to 300 characters.")
        if type(goal.get("completed")) is not bool:
            raise Problem("Timeline completed values must be true or false.")
        value = goal.get("due_at")
        try:
            if not isinstance(value, str):
                raise ValueError
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                day = date.fromisoformat(value)
                zone = current_app.extensions["zone"] if has_app_context() else UTC
                due = datetime.combine(day, time.min, tzinfo=zone)
            else:
                # Continue accepting previously downloaded version 1 plans.
                due = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if due.tzinfo is None or due.utcoffset() is None or not 1900 <= due.year <= 2199:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise Problem("Give each timeline goal a valid date in YYYY-MM-DD format.") from None
        identifier = goal.get("id", str(ObjectId()))
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{24}", identifier) or identifier in ids:
            raise Problem("Each timeline goal must have a unique 24-character hexadecimal ID, or omit its ID.")
        ids.add(identifier)
        goals.append({"id": identifier, "title": title.strip(), "due_at": due, "completed": goal["completed"]})
    return {"title": payload["title"].strip(), "body": payload["body"], "goals": sorted(goals, key=lambda goal: goal["due_at"])}


def timeline(plan, reference=None):
    reference = reference or now()
    if has_app_context():
        reference = reference.astimezone(current_app.extensions["zone"])
    reference_day = reference.date()
    rows = sorted(plan.get("goals", []), key=goal_date)
    pending = [row for row in rows if not row["completed"]]
    past = [row for row in pending if goal_date(row) <= reference_day]
    current = (past[-1] if past else pending[0]) if pending else None
    return [{**row, "due_date": goal_date(row).isoformat(), "state": "completed" if row["completed"] else (
        "current" if current and row["id"] == current["id"] else "future" if goal_date(row) > reference_day else "past"
    )} for row in rows]


def lesson_context(lesson):
    topic_id = lesson.get("topic_id")
    if not topic_id:
        # Legacy lessons can be matched only if the topic is unambiguous.
        topics = list(db().enrollments.find({"tutor_id": lesson["tutor_id"], "student_id": lesson["student_id"]}).limit(2))
        topic_id = topics[0]["_id"] if len(topics) == 1 else None
    topic = db().enrollments.find_one({"_id": topic_id, "tutor_id": lesson["tutor_id"], "student_id": lesson["student_id"]}) if topic_id else None
    plan = (db().course_plans.find_one({"_id": topic_id}) or {
        "_id": topic_id, "title": topic.get("topic", "General lessons"), "body": "", "goals": [], "revision": 0,
    }) if topic else None
    return {"course_topic": topic, "course_plan": plan, "course_timeline": timeline(plan) if plan else [], "ai_available": bool(current_app.config.get("OPENROUTER_API_KEY"))}


@bp.get("/course-plans")
@requires("tutor", "parent", "student")
def index():
    topics = list(db().enrollments.find(topic_scope()))
    names = people(row[key] for row in topics for key in ("student_id", "tutor_id"))
    plans = {row["_id"]: row for row in db().course_plans.find({"_id": {"$in": [topic["_id"] for topic in topics]}}, {"title": 1, "updated_at": 1})}
    return render_template("course_plans.html", topics=topics, names=names, plans=plans)


@bp.get("/course-plans/<topic_id>")
@requires("tutor", "parent", "student")
def detail(topic_id):
    topic = get_topic(topic_id)
    plan = get_plan(topic_id)
    files = list(db().course_plan_files.find({"topic_id": topic["_id"]}, {"data": 0}).sort("created_at", 1))
    return render_template("course_plan.html", topic=topic, plan=plan, timeline=timeline(plan),
                           names=people((topic["student_id"], topic["tutor_id"])), files=files,
                           file_accept=FILE_ACCEPT, schema=SCHEMA if g.user["role"] == "tutor" else None,
                           plan_json=export_plan(plan) if g.user["role"] == "tutor" else None)


def save_plan(topic, content, contents, revision):
    def save(transaction):
        lock_accounts(transaction, {topic["tutor_id"]: "tutor", topic["student_id"]: "student"})
        get_topic(topic["_id"], transaction)
        current = db().course_plans.find_one({"_id": topic["_id"]}, session=transaction)
        if (current or {}).get("revision", 0) != revision:
            raise Problem("The course plan changed in another tab. Reload before saving.", 409)
        if db().course_plan_files.count_documents({"topic_id": topic["_id"]}, session=transaction) + len(contents) > 30:
            raise Problem("Keep up to 30 files per course plan. Remove older files before adding more.")
        timestamp = now()
        db().course_plans.update_one({"_id": topic["_id"]}, {"$set": {
            **content, "tutor_id": topic["tutor_id"], "student_id": topic["student_id"],
            "updated_at": timestamp, "revision": revision + 1,
        }}, upsert=True, session=transaction)
        if contents:
            db().course_plan_files.insert_many([{"topic_id": topic["_id"], "created_at": timestamp, **item} for item in contents], session=transaction)
    atomic(save)


def form_revision():
    try:
        revision = int(request.form.get("revision", "0"))
        if not 0 <= revision < 2**63 - 1:
            raise ValueError
        return revision
    except ValueError:
        raise Problem("Reload the course plan before saving.") from None


@bp.post("/course-plans/<topic_id>")
@requires("tutor")
def save(topic_id):
    topic = get_topic(topic_id)
    try:
        goals = json.loads(request.form.get("goals", "[]"))
    except (ValueError, RecursionError):
        raise Problem("Reload the timeline before saving.") from None
    content = validate_plan({"version": 1, "title": request.form.get("title", ""), "body": request.form.get("body", ""), "goals": goals})
    save_plan(topic, content, selected_uploads("files", required=False), form_revision())
    flash("Course plan saved.")
    return redirect(url_for("planning.detail", topic_id=topic_id), code=303)


@bp.post("/course-plans/<topic_id>/import")
@requires("tutor")
def import_plan(topic_id):
    topic = get_topic(topic_id)
    upload = request.files.get("plan_file")
    if not upload or not upload.filename:
        raise Problem("Choose a course plan JSON file.")
    raw = upload.stream.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise Problem("Course plan JSON files must be 256 KB or smaller.", 413)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise Problem("Choose a readable course plan JSON file.") from None
    content = validate_plan(payload)
    save_plan(topic, content, [], form_revision())
    flash("Course plan imported. Existing attached files were kept.")
    return redirect(url_for("planning.detail", topic_id=topic_id), code=303)


@bp.get("/course-plans/<topic_id>/download")
@requires("tutor")
def download(topic_id):
    plan = get_plan(topic_id)
    return send_file(BytesIO(json.dumps(export_plan(plan), ensure_ascii=False, indent=2).encode()), mimetype="application/json", download_name="course-plan.json", as_attachment=True)


@bp.post("/course-plans/<topic_id>/delete")
@requires("tutor")
def delete(topic_id):
    topic = get_topic(topic_id)
    if request.form.get("confirmed") != "yes":
        raise Problem("Confirm that you want to delete this course plan.")
    revision = form_revision()

    def remove(transaction):
        lock_accounts(transaction, {topic["tutor_id"]: "tutor", topic["student_id"]: "student"})
        get_topic(topic["_id"], transaction)
        current = db().course_plans.find_one({"_id": topic["_id"]}, session=transaction)
        if not current:
            abort(404)
        if current.get("revision", 0) != revision:
            raise Problem("The course plan changed in another tab. Reload before deleting.", 409)
        discussion = {"kind": "course", "target_id": topic["_id"]}
        comment_ids = [row["_id"] for row in db().comments.find(discussion, {"_id": 1}, session=transaction)]
        db().comment_images.delete_many({"comment_id": {"$in": comment_ids}}, session=transaction)
        db().notifications.delete_many(discussion, session=transaction)
        db().comments.delete_many(discussion, session=transaction)
        db().course_plan_files.delete_many({"topic_id": topic["_id"]}, session=transaction)
        db().course_plans.delete_one({"_id": topic["_id"]}, session=transaction)

    atomic(remove)
    flash("Course plan deleted.")
    return redirect(url_for("planning.index"), code=303)


@bp.post("/course-plans/<topic_id>/goals/<goal_id>")
@requires("tutor")
def check_goal(topic_id, goal_id):
    topic = get_topic(topic_id)
    completed = request.form.get("completed") == "yes"
    result = db().course_plans.find_one_and_update({"_id": topic["_id"], "goals.id": goal_id}, {
        "$set": {"goals.$.completed": completed, "updated_at": now()}, "$inc": {"revision": 1},
    }, return_document=ReturnDocument.AFTER)
    if not result:
        abort(404)
    if request.headers.get("Accept") == "application/json":
        return {"completed": completed, "revision": result["revision"], "timeline": [{"id": row["id"], "state": row["state"], "completed": row["completed"]} for row in timeline(result)]}
    return redirect(url_for("planning.detail", topic_id=topic_id), code=303)


@bp.get("/course-plans/<topic_id>/files/<file_id>")
@requires("tutor", "parent", "student")
def file(topic_id, file_id):
    topic = get_topic(topic_id)
    item = db().course_plan_files.find_one({"_id": object_id(file_id), "topic_id": topic["_id"]})
    if not item:
        abort(404)
    return send_file(BytesIO(item["data"]), mimetype=item["mimetype"], download_name=item["filename"], as_attachment=True)


@bp.post("/course-plans/<topic_id>/files/<file_id>/remove")
@requires("tutor")
def remove_file(topic_id, file_id):
    topic = get_topic(topic_id)
    if not db().course_plan_files.delete_one({"_id": object_id(file_id), "topic_id": topic["_id"]}).deleted_count:
        abort(404)
    flash("Course plan file removed.")
    return redirect(url_for("planning.detail", topic_id=topic_id), code=303)


@bp.post("/lessons/<lesson_id>/plan")
@requires("tutor")
def save_lesson_plan(lesson_id):
    lesson = get_lesson(lesson_id)
    body = text_field(request.form, "lesson_plan", "the lesson plan", limit=MAX_BODY)
    # A revision check protects a tutor's draft against edits in another tab.
    expected = request.form.get("plan_revision", "0")
    try:
        revision = int(expected)
        if not 0 <= revision < 2**63 - 1:
            raise ValueError
    except ValueError:
        raise Problem("Reload the lesson before saving its plan.") from None
    query = {"_id": lesson["_id"], "$or": [{"plan_revision": revision}] + ([{"plan_revision": {"$exists": False}}] if revision == 0 else [])}
    if not db().lessons.update_one(query, {"$set": {"lesson_plan": body, "plan_updated_at": now()}, "$inc": {"plan_revision": 1}}).matched_count:
        raise Problem("The lesson plan changed in another tab. Reload before saving.", 409)
    flash("Lesson plan saved." if body else "Lesson plan cleared.")
    return redirect(url_for("lessons.detail", lesson_id=lesson_id), code=303)
