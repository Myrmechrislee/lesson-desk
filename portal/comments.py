"""Private, resource-scoped discussions and durable in-app notifications."""
from io import BytesIO

from bson import ObjectId
from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for

from .core import Problem, atomic, db, lock_accounts, now, object_id, requires, text_field
from .uploads import prepare_photo

bp = Blueprint("comments", __name__)
KINDS = {"lesson", "course", "announcement"}


def _family_id(user):
    return user["_id"] if user["role"] == "parent" else user.get("parent_id", user["_id"])


def _student_scope(transaction=None):
    if g.user["role"] == "parent":
        ids = [row["_id"] for row in db().users.find({
            "role": "student", "active": True, "parent_id": g.user["_id"],
        }, {"_id": 1}, session=transaction)]
        return {"student_id": {"$in": ids}}
    return {f"{g.user['role']}_id": g.user["_id"]}


def get_target(kind, target_id, transaction=None):
    """Recheck membership on every operation, including image downloads."""
    if kind not in KINDS:
        abort(404)
    query = {"_id": object_id(target_id)}
    if kind == "announcement":
        target = db().announcements.find_one(query, session=transaction)
    else:
        if g.user["role"] not in {"tutor", "student", "parent"}:
            abort(404)
        query.update(_student_scope(transaction))
        collection = db().lessons if kind == "lesson" else db().enrollments
        target = collection.find_one(query, session=transaction)
    if not target:
        abort(404)
    return target


def target_url(kind, target_id):
    endpoints = {
        "lesson": ("lessons.detail", "lesson_id"),
        "course": ("planning.detail", "topic_id"),
        "announcement": ("announcements.detail", "post_id"),
    }
    endpoint, key = endpoints[kind]
    return url_for(endpoint, **{key: target_id})


def visible_query(kind, target_id, transaction=None):
    query = {"kind": kind, "target_id": object_id(target_id)}
    if kind == "announcement" and g.user["role"] not in {"admin", "tutor"}:
        family_id = _family_id(g.user)
        family_ids = [family_id] + [row["_id"] for row in db().users.find({
            "role": "student", "active": True, "parent_id": family_id,
        }, {"_id": 1}, session=transaction)]
        if g.user["_id"] not in family_ids:
            family_ids.append(g.user["_id"])
        # An account reassignment must not leave an old household with access to
        # that student's threads or private attachments.
        roots = [row["_id"] for row in db().comments.find({
            **query, "root_id": None, "family_id": family_id,
            "author_id": {"$in": family_ids},
        }, {"_id": 1}, session=transaction)]
        query.update({"family_id": family_id, "$or": [
            {"_id": {"$in": roots}},
            {"root_id": {"$in": roots}, "$or": [
                {"author_id": {"$in": family_ids}}, {"author_role": {"$in": ["admin", "tutor"]}},
            ]},
        ]})
    return query


def get_comment(kind, target_id, comment_id, transaction=None):
    comment = db().comments.find_one({
        "_id": object_id(comment_id), **visible_query(kind, target_id, transaction),
    }, session=transaction)
    if not comment:
        abort(404)
    return comment


def comment_thread(kind, target_id):
    target = get_target(kind, target_id)
    rows = list(db().comments.find(visible_query(kind, target_id)).sort([("created_at", 1), ("_id", 1)]))
    ids = [row["_id"] for row in rows]
    images = list(db().comment_images.find({"comment_id": {"$in": ids}}, {"data": 0}).sort("_id", 1))
    by_id = {row["_id"]: row for row in rows}
    for row in rows:
        row["images"] = [image for image in images if image["comment_id"] == row["_id"]]
        row["replies"] = []
    roots = []
    for row in rows:
        # Replies always point to a root, so discussions stay readable and bounded.
        parent = by_id.get(row.get("root_id"))
        if parent is not None:
            parent["replies"].append(row)
        elif not row.get("root_id"):
            roots.append(row)
    return {"target": target, "threads": roots, "kind": kind, "target_id": target["_id"]}


@bp.app_context_processor
def discussion_context():
    user = getattr(g, "user", None)
    return {
        "comment_thread": comment_thread,
        "unread_notification_count": db().notifications.count_documents({
            "user_id": user["_id"], "read_at": None,
        }) if user else 0,
    }


def _images():
    uploads = [upload for upload in request.files.getlist("images") if upload.filename]
    if len(uploads) > 5:
        raise Problem("Attach up to five images to a comment.")
    return [prepare_photo(upload) for upload in uploads]


def _notify(kind, target, comment, transaction):
    recipients = set()
    if kind == "announcement":
        recipients.update(row["_id"] for row in db().users.find({
            "role": "admin", "active": True,
        }, {"_id": 1}, session=transaction))
    else:
        recipients.add(target["tutor_id"])
    if comment.get("root_id"):
        # Notify participants about replies without exposing other families' threads.
        participants = db().comments.find({
            "kind": kind, "target_id": target["_id"],
            "$or": [{"_id": comment["root_id"]}, {"root_id": comment["root_id"]}],
        }, {"author_id": 1}, session=transaction)
        recipients.update(row["author_id"] for row in participants)
    recipients.discard(g.user["_id"])
    title = target.get("title") or target.get("topic") or "Course plan"
    if recipients:
        db().notifications.insert_many([{
            "user_id": recipient, "kind": kind, "target_id": target["_id"],
            "comment_id": comment["_id"], "created_at": now(), "read_at": None,
            "message": f"{g.user['name']} commented on {title}.",
        } for recipient in recipients], session=transaction)


@bp.post("/comments/<kind>/<target_id>")
@requires("admin", "tutor", "parent", "student")
def create(kind, target_id):
    get_target(kind, target_id)
    body = text_field(request.form, "body", "comment text", limit=6000)
    images = _images()
    if not body and not images:
        raise Problem("Write a comment or attach an image.")
    parent_id = request.form.get("reply_to", "").strip()

    def persist(transaction):
        lock_accounts(transaction, {g.user["_id"]: g.user["role"]})
        target = get_target(kind, target_id, transaction)
        parent = get_comment(kind, target_id, parent_id, transaction) if parent_id else None
        family_id = parent.get("family_id") if parent else (
            _family_id(g.user) if g.user["role"] in {"parent", "student"} else None
        )
        comment = {
            "_id": ObjectId(), "kind": kind, "target_id": target["_id"],
            "author_id": g.user["_id"], "author_name": g.user["name"],
            "author_role": g.user["role"], "body": body, "family_id": family_id,
            "root_id": (parent.get("root_id") or parent["_id"]) if parent else None,
            "reply_to": parent["_id"] if parent else None,
            "created_at": now(), "edited_at": None,
        }
        db().comments.insert_one(comment, session=transaction)
        if images:
            db().comment_images.insert_many([{
                "comment_id": comment["_id"], "data": data,
            } for data in images], session=transaction)
        _notify(kind, target, comment, transaction)
        return comment["_id"]

    comment_id = atomic(persist)
    flash("Comment posted.")
    return redirect(target_url(kind, target_id) + f"#comment-{comment_id}", code=303)


@bp.post("/comments/<kind>/<target_id>/<comment_id>/edit")
@requires("admin", "tutor", "parent", "student")
def edit(kind, target_id, comment_id):
    get_target(kind, target_id)
    existing = get_comment(kind, target_id, comment_id)
    if existing["author_id"] != g.user["_id"]:
        abort(403)
    body = text_field(request.form, "body", "comment text", limit=6000)
    images = _images()
    removed = {object_id(value) for value in request.form.getlist("remove_images")}

    def persist(transaction):
        lock_accounts(transaction, {g.user["_id"]: g.user["role"]})
        get_target(kind, target_id, transaction)
        current = get_comment(kind, target_id, comment_id, transaction)
        if current["author_id"] != g.user["_id"]:
            abort(403)
        image_query = {"comment_id": current["_id"]}
        current_images = {row["_id"] for row in db().comment_images.find(image_query, {"_id": 1}, session=transaction)}
        if not removed.issubset(current_images):
            abort(404)
        count = len(current_images - removed) + len(images)
        if count > 5:
            raise Problem("Keep a comment to five images or fewer.")
        if not body and not count:
            raise Problem("Write a comment or attach an image.")
        db().comments.update_one({"_id": current["_id"]}, {"$set": {
            "body": body, "edited_at": now(),
        }}, session=transaction)
        if removed:
            db().comment_images.delete_many({**image_query, "_id": {"$in": list(removed)}}, session=transaction)
        if images:
            db().comment_images.insert_many([{
                "comment_id": current["_id"], "data": data,
            } for data in images], session=transaction)

    atomic(persist)
    flash("Comment updated.")
    return redirect(target_url(kind, target_id) + f"#comment-{comment_id}", code=303)


@bp.get("/comments/<kind>/<target_id>/<comment_id>/images/<image_id>")
@requires("admin", "tutor", "parent", "student")
def image(kind, target_id, comment_id, image_id):
    get_target(kind, target_id)
    comment = get_comment(kind, target_id, comment_id)
    image = db().comment_images.find_one({"_id": object_id(image_id), "comment_id": comment["_id"]})
    if not image:
        abort(404)
    return send_file(BytesIO(image["data"]), mimetype="image/jpeg", download_name="comment-image.jpg")


@bp.get("/notifications")
@requires("admin", "tutor", "parent", "student")
def notifications():
    rows = list(db().notifications.find({"user_id": g.user["_id"]}).sort([("created_at", -1), ("_id", -1)]).limit(100))
    return render_template("notifications.html", notifications=rows)


@bp.post("/notifications/<notification_id>/read")
@requires("admin", "tutor", "parent", "student")
def read_notification(notification_id):
    notification = db().notifications.find_one({"_id": object_id(notification_id), "user_id": g.user["_id"]})
    if not notification:
        abort(404)
    get_target(notification["kind"], notification["target_id"])
    get_comment(notification["kind"], notification["target_id"], notification["comment_id"])
    db().notifications.update_one({"_id": notification["_id"], "user_id": g.user["_id"]}, {"$set": {"read_at": now()}})
    return redirect(target_url(notification["kind"], notification["target_id"]) + f"#comment-{notification['comment_id']}", code=303)


@bp.post("/notifications/read-all")
@requires("admin", "tutor", "parent", "student")
def read_all():
    db().notifications.update_many({"user_id": g.user["_id"], "read_at": None}, {"$set": {"read_at": now()}})
    return redirect(url_for("comments.notifications"), code=303)
