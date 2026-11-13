from datetime import timedelta
from io import BytesIO
import secrets

from bson import ObjectId
from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for

from .core import Problem, atomic, db, local_datetime, lock_accounts, now, object_id, people, requires, text_field
from .uploads import FILE_ACCEPT, selected_uploads

bp = Blueprint("announcements", __name__)
FILE_FIELDS = {"data": 0}


def read_version(post):
    return post.get("read_version") or post.get("updated_at") or post.get("created_at")


def mark_viewed(user, posts):
    for post in posts:
        db().announcement_views.update_one({"user_id": user["_id"], "announcement_id": post["_id"]},
                                         {"$set": {"version": read_version(post), "read_at": now()}}, upsert=True)
        if user["role"] == "parent" and post.get("popup"):
            db().announcement_reads.update_one({"parent_id": user["_id"], "announcement_id": post["_id"]},
                                             {"$set": {"version": post.get("popup_version"), "read_at": now()}}, upsert=True)


def with_files(posts):
    files = list(db().announcement_files.find({"announcement_id": {"$in": [post["_id"] for post in posts]}}, FILE_FIELDS).sort("created_at", 1))
    for post in posts:
        post["files"] = [file for file in files if file["announcement_id"] == post["_id"]]
    return posts


def notices_for(user):
    if user["role"] not in {"parent", "tutor", "admin", "student"}:
        return {"announcement_banners": [], "announcement_popups": [], "unread_announcement_count": 0}
    posts = list(db().announcements.find({}, {"read_version": 1, "updated_at": 1, "created_at": 1, "popup_version": 1}))
    views = {row["announcement_id"]: row.get("version") for row in db().announcement_views.find({"user_id": user["_id"]})}
    reads = {row["announcement_id"]: row.get("version") for row in db().announcement_reads.find({"parent_id": user["_id"]})} if user["role"] == "parent" else {}
    unread = sum(1 for post in posts
                 if (post["_id"] not in views or views[post["_id"]] != read_version(post))
                 and not (post.get("popup_version") and reads.get(post["_id"]) == post["popup_version"]))
    timestamp = now()
    banners = list(db().announcements.find({
        "banner": True, "banner_start": {"$lte": timestamp}, "banner_end": {"$gt": timestamp},
    }, {"title": 1}).sort("created_at", -1))
    popups = []
    if user["role"] == "parent":
        candidates = list(db().announcements.find({"important": True, "popup": True}).sort("created_at", -1))
        popups = with_files([post for post in candidates if reads.get(post["_id"]) != post.get("popup_version")])
    return {"announcement_banners": banners, "announcement_popups": popups, "unread_announcement_count": unread}


def get_post(post_id, transaction=None):
    post = db().announcements.find_one({"_id": object_id(post_id)}, session=transaction)
    if not post:
        abort(404)
    return post


@bp.get("/announcements")
@requires("admin", "tutor", "parent", "student")
def index():
    posts = with_files(list(db().announcements.find().sort([("created_at", -1), ("_id", -1)])))
    mark_viewed(g.user, posts)
    names = people(post["created_by"] for post in posts)
    return render_template("announcements.html", posts=posts, names=names)


@bp.get("/announcements/new")
@requires("admin", "tutor")
def new():
    return render_template("announcement_form.html", post=None, file_accept=FILE_ACCEPT,
                           default_start=now(), default_end=now() + timedelta(days=7))


@bp.get("/announcements/<post_id>")
@requires("admin", "tutor", "parent", "student")
def detail(post_id):
    post = with_files([get_post(post_id)])[0]
    mark_viewed(g.user, [post])
    return render_template("announcement_detail.html", post=post, author=people([post["created_by"]])[post["created_by"]])


@bp.get("/announcements/<post_id>/edit")
@requires("admin", "tutor")
def edit(post_id):
    return render_template("announcement_form.html", post=with_files([get_post(post_id)])[0],
                           file_accept=FILE_ACCEPT, default_start=now(), default_end=now() + timedelta(days=7))


@bp.post("/announcements/new")
@bp.post("/announcements/<post_id>/edit")
@requires("admin", "tutor")
def save(post_id=None):
    important = request.form.get("important") == "yes"
    popup = request.form.get("popup") == "yes"
    banner = request.form.get("banner") == "yes"
    if popup and not important:
        raise Problem("Mark the announcement as important to show a pop-up to parents.")
    banner_start = local_datetime(request.form.get("banner_start", "")) if banner else None
    banner_end = local_datetime(request.form.get("banner_end", "")) if banner else None
    if banner and banner_end <= banner_start:
        raise Problem("The banner end must be after its start.")
    contents = selected_uploads("files", required=False)
    removed_ids = [object_id(value) for value in request.form.getlist("remove_files")]
    if removed_ids and not post_id:
        raise Problem("Save the announcement before removing attachments.")
    fields = {
        "title": text_field(request.form, "title", "an announcement title", limit=120, required=True),
        "body": text_field(request.form, "body", "announcement text", limit=6000, required=True),
        "updated_at": now(), "updated_by": g.user["_id"], "read_version": secrets.token_hex(16),
        "important": important, "popup": popup, "popup_version": secrets.token_hex(16) if popup else None,
        "banner": banner, "banner_start": banner_start, "banner_end": banner_end,
    }

    def persist(transaction):
        lock_accounts(transaction, {g.user["_id"]: g.user["role"]})
        if post_id:
            post = get_post(post_id, transaction)
            post_key = post["_id"]
            if db().announcement_files.count_documents({"announcement_id": post_key, "_id": {"$in": removed_ids}}, session=transaction) != len(set(removed_ids)):
                abort(404)
            count = db().announcement_files.count_documents({"announcement_id": post_key}, session=transaction)
            if count - len(set(removed_ids)) + len(contents) > 20:
                raise Problem("Keep an announcement to 20 attachments or fewer.")
            db().announcements.update_one({"_id": post["_id"]}, {"$set": fields}, session=transaction)
            db().announcement_files.delete_many({"announcement_id": post_key, "_id": {"$in": removed_ids}}, session=transaction)
        else:
            post_key = ObjectId()
            db().announcements.insert_one({
                "_id": post_key, **fields, "created_at": fields["updated_at"], "created_by": g.user["_id"],
            }, session=transaction)
        if contents:
            db().announcement_files.insert_many([{
                "announcement_id": post_key, "created_at": now(), "uploaded_by": g.user["_id"], **content,
            } for content in contents], session=transaction)

    atomic(persist)
    flash("Announcement updated." if post_id else "Announcement posted.")
    return redirect(url_for("announcements.index"), code=303)


@bp.get("/announcements/<post_id>/files/<file_id>")
@requires("admin", "tutor", "parent", "student")
def download(post_id, file_id):
    get_post(post_id)
    file = db().announcement_files.find_one({"_id": object_id(file_id), "announcement_id": object_id(post_id)})
    if not file:
        abort(404)
    return send_file(BytesIO(file["data"]), mimetype=file["mimetype"], as_attachment=True, download_name=file["filename"])


@bp.post("/announcements/<post_id>/acknowledge")
@requires("parent")
def acknowledge(post_id):
    post = get_post(post_id)
    version = text_field(request.form, "version", "the announcement version", limit=100, required=True)
    if not post.get("popup") or version != post.get("popup_version"):
        raise Problem("This announcement has changed. Reload it to read the latest version.", 409)
    db().announcement_reads.update_one({"parent_id": g.user["_id"], "announcement_id": post["_id"]}, {"$set": {
        "version": version, "read_at": now(),
    }}, upsert=True)
    mark_viewed(g.user, [post])
    if request.headers.get("Accept") == "application/json":
        return "", 204
    return redirect(url_for("announcements.detail", post_id=post_id), code=303)


@bp.post("/announcements/<post_id>/remove")
@requires("admin", "tutor")
def remove(post_id):
    def delete(transaction):
        lock_accounts(transaction, {g.user["_id"]: g.user["role"]})
        post = get_post(post_id, transaction)
        db().announcements.delete_one({"_id": post["_id"]}, session=transaction)
        db().announcement_files.delete_many({"announcement_id": post["_id"]}, session=transaction)
        db().announcement_reads.delete_many({"announcement_id": post["_id"]}, session=transaction)
        db().announcement_views.delete_many({"announcement_id": post["_id"]}, session=transaction)

    atomic(delete)
    flash("Announcement removed.")
    return redirect(url_for("announcements.index"), code=303)
