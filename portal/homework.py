from io import BytesIO
import json
from time import monotonic, sleep

from flask import (
    Blueprint, Response, abort, current_app, flash, g, redirect, render_template,
    request, send_file, session, stream_with_context, url_for,
)
from pymongo.errors import PyMongoError

from .core import Problem, atomic, date_field, db, lock_accounts, now, object_id, people, requires, text_field
from .lessons import enrollment_for, tutor_students
from .uploads import FILE_ACCEPT, selected_uploads

bp = Blueprint("homework", __name__)
PHOTO_FIELDS = {"created_at": 1, "homework_id": 1, "kind": 1, "filename": 1, "mimetype": 1}


def attachment_query(homework_id, kind="submission"):
    query = {"homework_id": homework_id}
    # Existing photos predate attachment kinds and are student submissions.
    query["kind"] = {"$in": [None, "submission"]} if kind == "submission" else kind
    return query


def photo_rows(homework_id, kind="submission"):
    return list(db().homework_photos.find(attachment_query(homework_id, kind), PHOTO_FIELDS).sort(
        [("created_at", 1), ("_id", 1)],
    ))


def photo_counts(homework_ids):
    groups = db().homework_photos.aggregate([
        {"$match": {"homework_id": {"$in": homework_ids}, "kind": {"$in": [None, "submission"]}}},
        {"$group": {"_id": "$homework_id", "count": {"$sum": 1}}},
    ])
    return {row["_id"]: row["count"] for row in groups}


def assignment_scope(transaction=None):
    role = g.user["role"]
    if role == "parent":
        children = db().users.find(
            {"role": "student", "active": True, "parent_id": g.user["_id"]},
            {"_id": 1}, session=transaction,
        )
        return {"student_id": {"$in": [child["_id"] for child in children]}}
    return {f"{role}_id": g.user["_id"]}


def get_assignment(homework_id, transaction=None):
    assignment = db().homework.find_one({
        "_id": object_id(homework_id), **assignment_scope(transaction),
    }, session=transaction)
    if not assignment:
        abort(404)
    return assignment


def lock_assignment(assignment, transaction):
    roles = {assignment["student_id"]: "student", g.user["_id"]: g.user["role"]}
    lock_accounts(transaction, roles)
    return get_assignment(assignment["_id"], transaction)


@bp.get("/homework")
@requires("tutor", "student", "parent")
def index():
    rows = list(db().homework.find(assignment_scope()).sort("created_at", -1))
    names = people(user_id for row in rows for user_id in (row["tutor_id"], row["student_id"]))
    counts = photo_counts([row["_id"] for row in rows])
    return render_template("homework.html", assignments=rows, names=names, counts=counts)


@bp.get("/homework/new")
@requires("tutor")
def new():
    enrollments, names = tutor_students()
    return render_template("homework_new.html", enrollments=enrollments, names=names, file_accept=FILE_ACCEPT)



def save_attachments(assignment, contents, kind, transaction):
    if not contents:
        return
    timestamp = now()
    db().homework_photos.insert_many([
        {
            "homework_id": assignment["_id"], "tutor_id": assignment["tutor_id"],
            "created_at": timestamp, "uploaded_by": g.user["_id"], "kind": kind, **content,
        }
        for content in contents
    ], session=transaction)


@bp.post("/homework/new")
@requires("tutor")
def create():
    tutor_id = g.user["_id"]
    student_id = object_id(request.form.get("student_id"))
    if tutor_id == student_id:
        raise Problem("Choose a student account.")
    title = text_field(request.form, "title", "a homework title", limit=100, required=True)
    instructions = text_field(request.form, "instructions", "homework instructions", limit=4000, required=True)
    due = request.form.get("due", "")
    due_on = date_field(due).isoformat() if due else None
    contents = selected_uploads("files", required=False)

    def assign(transaction):
        lock_accounts(transaction, {tutor_id: "tutor", student_id: "student"})
        enrollment_for(tutor_id, student_id, transaction)
        assignment = {
            "tutor_id": tutor_id, "student_id": student_id, "title": title,
            "instructions": instructions, "due_on": due_on, "created_at": now(),
        }
        assignment["_id"] = db().homework.insert_one(assignment, session=transaction).inserted_id
        save_attachments(assignment, contents, "assignment", transaction)
        return assignment["_id"]

    homework_id = atomic(assign)
    flash("Homework assigned.")
    return redirect(url_for("homework.detail", homework_id=homework_id), code=303)


@bp.get("/homework/<homework_id>")
@requires("tutor", "student", "parent")
def detail(homework_id):
    assignment = get_assignment(homework_id)
    names = people((assignment["student_id"], assignment["tutor_id"]))
    return render_template(
        "homework_detail.html", assignment=assignment, names=names,
        photos=photo_rows(assignment["_id"]), resources=photo_rows(assignment["_id"], "assignment"),
        marked=photo_rows(assignment["_id"], "marked"), file_accept=FILE_ACCEPT,
    )



@bp.post("/homework/<homework_id>/photos")
@bp.post("/homework/<homework_id>/files")
@requires("student", "parent")
def upload(homework_id):
    assignment = get_assignment(homework_id)
    contents = selected_uploads("files", "photos")

    def save(transaction):
        current = lock_assignment(assignment, transaction)
        save_attachments(current, contents, "submission", transaction)
        db().homework.update_one({"_id": current["_id"]}, {"$unset": {"submitted_at": "", "submission_source": ""}}, session=transaction)

    atomic(save)
    flash("Work uploaded. Submit completed homework when it is ready for marking.")
    return redirect(url_for("homework.detail", homework_id=homework_id), code=303)


@bp.post("/homework/<homework_id>/submit")
@requires("student", "parent")
def submit(homework_id):
    assignment = get_assignment(homework_id)
    notes = text_field(request.form, "submission_notes", "homework notes", limit=4000)
    action = request.form.get("action", "submit")
    if action not in {"save", "submit"}:
        raise Problem("Choose whether to save notes or submit completed homework.")

    def save(transaction):
        current = lock_assignment(assignment, transaction)
        if action == "submit" and not notes and not db().homework_photos.find_one(
            attachment_query(current["_id"]), {"_id": 1}, session=transaction,
        ):
            raise Problem("Upload your work or write homework notes before submitting.")
        update = {"$set": {"submission_notes": notes, "submitted_by": g.user["_id"]}}
        if action == "submit":
            update["$set"]["submitted_at"] = now()
            update["$set"]["submission_source"] = "family"
        else:
            update["$unset"] = {"submitted_at": "", "submission_source": ""}
        db().homework.update_one({"_id": current["_id"]}, update, session=transaction)

    atomic(save)
    flash("Completed homework submitted for marking." if action == "submit" else "Homework notes saved.")
    return redirect(url_for("homework.detail", homework_id=homework_id), code=303)


@bp.post("/homework/<homework_id>/marked")
@requires("tutor")
def return_marked(homework_id):
    assignment = get_assignment(homework_id)
    contents = selected_uploads("files", required=False)
    feedback = text_field(request.form, "marking_feedback", "homework feedback", limit=4000)
    grade = text_field(request.form, "grade", "a grade", limit=100)
    complete_without_submission = request.form.get("complete_without_submission") == "yes"

    def save(transaction):
        current = lock_assignment(assignment, transaction)
        timestamp = now()
        completion = {}
        if not current.get("submitted_at"):
            if not complete_without_submission:
                raise Problem("Select completed offline to mark this homework without a student or parent submission.", 409)
            completion = {"submitted_at": timestamp, "submitted_by": g.user["_id"], "submission_source": "tutor_override"}
        save_attachments(current, contents, "marked", transaction)
        db().homework.update_one({"_id": current["_id"]}, {"$set": {
            **completion, "marked_at": timestamp, "marked_by": g.user["_id"],
            "marked_submission_at": current.get("submitted_at") or timestamp,
            "marking_feedback": feedback, "grade": grade,
        }}, session=transaction)

    atomic(save)
    flash("Homework marked as completed.")
    return redirect(url_for("homework.detail", homework_id=homework_id), code=303)


@bp.post("/homework/<homework_id>/photos/<photo_id>/delete")
@bp.post("/homework/<homework_id>/files/<photo_id>/delete")
@requires("student", "parent")
def delete_file(homework_id, photo_id):
    assignment = get_assignment(homework_id)
    item_id = object_id(photo_id)

    def remove(transaction):
        current = lock_assignment(assignment, transaction)
        result = db().homework_photos.delete_one({
            "_id": item_id, **attachment_query(current["_id"]),
        }, session=transaction)
        if not result.deleted_count:
            abort(404)
        db().homework.update_one({"_id": current["_id"]}, {"$unset": {"submitted_at": "", "submission_source": ""}}, session=transaction)

    atomic(remove)
    flash("Uploaded file removed. Submit completed homework again when it is ready.")
    return redirect(url_for("homework.detail", homework_id=homework_id), code=303)


@bp.get("/homework/<homework_id>/photos/<photo_id>")
@bp.get("/homework/<homework_id>/files/<photo_id>")
@requires("tutor", "student", "parent")
def photo(homework_id, photo_id):
    assignment = get_assignment(homework_id)
    item = db().homework_photos.find_one({
        "_id": object_id(photo_id), "homework_id": assignment["_id"],
    }, {"data": 1, "filename": 1, "mimetype": 1})
    if not item:
        abort(404)
    mimetype = item.get("mimetype", "image/jpeg")
    return send_file(
        BytesIO(item["data"]), mimetype=mimetype, download_name=item.get("filename", "homework-photo.jpg"),
        as_attachment=mimetype != "image/jpeg",
    )


def event_stream(event_name, match, snapshot):
    tutor_id = g.user["_id"]
    auth_expires_at = session.get("auth_expires_at", 0)
    database = db()

    def event():
        return f"event: {event_name}\ndata: {json.dumps(snapshot(), ensure_ascii=True)}\n\n"

    def still_authorized():
        return now().timestamp() < auth_expires_at and database.users.find_one(
            {"_id": tutor_id, "role": "tutor", "active": True}, {"_id": 1},
        )

    @stream_with_context
    def stream():
        deadline = monotonic() + 27
        yield "retry: 1500\n\n"
        try:
            pipeline = [
                {"$match": {"$or": [
                    {"operationType": {"$in": ["insert", "replace", "update"]}, **match},
                    # Deletes have no fullDocument; refresh this tutor's authorized snapshot.
                    {"operationType": "delete"},
                ]}},
                {"$project": {"_id": 1, "fullDocument.homework_id": 1}},
            ]
            with database.homework_photos.watch(pipeline, max_await_time_ms=1200) as changes:
                yield event()
                heartbeat = monotonic() + 8
                while monotonic() < deadline:
                    change = changes.try_next()
                    if change or monotonic() >= heartbeat:
                        if not still_authorized():
                            return
                        heartbeat = monotonic() + 8
                        yield event() if change else ": keepalive\n\n"
        except PyMongoError:
            current_app.logger.warning("Homework change stream disconnected. Using periodic refresh.")
            while monotonic() < deadline and still_authorized():
                yield event()
                sleep(3)

    return Response(stream(), mimetype="text/event-stream", headers={"X-Accel-Buffering": "no"})


@bp.get("/homework/live")
@requires("tutor")
def live():
    tutor_id = g.user["_id"]

    def snapshot():
        ids = [row["_id"] for row in db().homework.find({"tutor_id": tutor_id}, {"_id": 1})]
        counts = photo_counts(ids)
        return {str(assignment_id): counts.get(assignment_id, 0) for assignment_id in ids}

    return event_stream("counts", {"fullDocument.tutor_id": tutor_id}, snapshot)


@bp.get("/homework/<homework_id>/live")
@requires("tutor")
def live_photos(homework_id):
    assignment = get_assignment(homework_id)

    def snapshot():
        return [
            {
                "id": str(row["_id"]), "filename": row.get("filename", "Homework photo"),
                "is_image": row.get("mimetype", "image/jpeg") == "image/jpeg",
                "url": url_for("homework.photo", homework_id=homework_id, photo_id=row["_id"]),
            }
            for row in photo_rows(assignment["_id"])
        ]

    return event_stream("photos", {"fullDocument.homework_id": assignment["_id"]}, snapshot)
