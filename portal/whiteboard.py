"""Lesson-scoped collaborative boards and rasterized teaching resources."""
from io import BytesIO
from datetime import timedelta
import json
import math
import re
from threading import Lock

from bson import Binary, ObjectId
from flask import Blueprint, abort, g, jsonify, render_template, request, send_file
from PIL import Image
from pymongo.errors import DuplicateKeyError
from werkzeug.utils import secure_filename

from .core import Problem, atomic, db, now, object_id, people, requires, zone
from .calls import ice_servers
from .lessons import get_lesson
from .uploads import MAX_PHOTO_BYTES, prepare_photo

bp = Blueprint("whiteboard", __name__)
PDF_LOCK = Lock()  # PDFium cannot be called concurrently by gunicorn threads.
MAX_OBJECTS = 1500
MAX_ASSETS = 200
KINDS = {"path", "line", "rect", "ellipse", "polygon", "text", "note", "image", "template"}
TEMPLATES = {"cartesian", "cartesian-blank", "trig", "unit-circle", "two-way", "venn"}
ID_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")
PRESENCE_SECONDS = 30
PREVIEW_SECONDS = 8


def accessible_lesson(lesson_id):
    lesson = get_lesson(lesson_id)
    if g.user["role"] in {"parent", "student"} and lesson["start_at"] > now():
        raise Problem("The whiteboard opens when the lesson starts.", 403)
    return lesson


def snapshot(board):
    return {"revision": board["revision"], "elements": list(board["elements"].values())}


def load_board(lesson_id):
    db().whiteboards.update_one({"_id": lesson_id}, {"$setOnInsert": {
        "revision": 0, "elements": {}, "updated_at": now(),
    }}, upsert=True)
    return db().whiteboards.find_one({"_id": lesson_id})


def number(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum or not math.isfinite(value):
        raise Problem("The whiteboard contains an invalid position or size.")
    return value


def clean_element(element, element_id, lesson_id):
    if not isinstance(element, dict) or not isinstance(element.get("type"), str) or element["type"] not in KINDS:
        raise Problem("Choose a supported whiteboard object.")
    kind = element["type"]
    color = element.get("color", "#243d35")
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise Problem("Choose a valid drawing colour.")
    result = {"id": element_id, "type": kind, "color": color,
              "x": number(element.get("x"), -100000, 100000),
              "y": number(element.get("y"), -100000, 100000),
              "w": number(element.get("w", 1), 1, 10000),
              "h": number(element.get("h", 1), 1, 10000),
              "width": number(element.get("width", 3), 1, 16)}
    result["z"] = number(element.get("z", 0), -1e15, 1e15)
    if kind in {"rect", "ellipse", "polygon"}:
        fill = element.get("fill", "transparent")
        if not isinstance(fill, str) or (fill != "transparent" and not re.fullmatch(r"#[0-9a-fA-F]{6}", fill)):
            raise Problem("Choose a valid shape fill colour.")
        result["fill"] = fill
    if kind == "polygon":
        sides = element.get("sides")
        if type(sides) is not int or not 3 <= sides <= 12:
            raise Problem("Choose a shape with three to twelve sides.")
        result["sides"] = sides
    if kind in {"text", "note"}:
        text = element.get("text", "")
        if not isinstance(text, str) or len(text) > 8000 or "\x00" in text:
            raise Problem("Keep each text object to 8,000 characters.")
        result.update(text=text, font=number(element.get("font", 24), 12, 120))
    if kind == "path":
        points = element.get("points")
        if not isinstance(points, list) or not 1 <= len(points) <= 4000:
            raise Problem("This stroke is too long. Draw it in smaller sections.")
        result["points"] = []
        for point in points:
            if not isinstance(point, list) or len(point) != 2:
                raise Problem("The whiteboard contains an invalid stroke.")
            result["points"].append([number(v, -10000, 10000) for v in point])
    if kind == "line":
        result["dx"] = number(element.get("dx", result["w"]), -10000, 10000)
        result["dy"] = number(element.get("dy", result["h"]), -10000, 10000)
    if kind == "image":
        asset_id = object_id(element.get("asset_id"))
        if not db().whiteboard_assets.find_one({"_id": asset_id, "lesson_id": lesson_id}, {"_id": 1}):
            raise Problem("That resource does not belong to this lesson.")
        result["asset_id"] = str(asset_id)
    if kind == "template":
        if not isinstance(element.get("template"), str) or element["template"] not in TEMPLATES:
            raise Problem("Choose a maths template.")
        result["template"] = element["template"]
    return result


@bp.get("/lessons/<lesson_id>/whiteboard")
@requires("tutor", "parent", "student")
def board(lesson_id):
    lesson = accessible_lesson(lesson_id)
    student = people([lesson["student_id"]]).get(lesson["student_id"])
    return render_template("whiteboard.html", lesson=lesson,
                           student_name=student["name"] if student else "Student",
                           lesson_date=lesson["start_at"].astimezone(zone()).date().isoformat(),
                           call_enabled=lesson["status"] not in {"cancelled", "cancelled_late"},
                           call_ice_servers=ice_servers() if lesson["status"] not in {"cancelled", "cancelled_late"} else [])


@bp.get("/lessons/<lesson_id>/whiteboard/state")
@requires("tutor", "parent", "student")
def state(lesson_id):
    lesson = accessible_lesson(lesson_id)
    board = load_board(lesson["_id"])
    if request.args.get("since") == str(board["revision"]):
        return {"revision": board["revision"], "unchanged": True}
    return snapshot(board)


@bp.post("/lessons/<lesson_id>/whiteboard/state")
@requires("tutor", "parent", "student")
def save(lesson_id):
    lesson = accessible_lesson(lesson_id)
    if request.content_length and request.content_length > 2 * 1024 * 1024:
        raise Problem("Save smaller groups of whiteboard changes.", 413)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or type(payload.get("revision")) is not int:
        raise Problem("Reload the whiteboard before saving.")
    changes = payload.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 200:
        raise Problem("Save one to 200 whiteboard changes at a time.")
    board = load_board(lesson["_id"])
    if board["revision"] != payload["revision"]:
        return jsonify(snapshot(board)), 409
    elements = dict(board["elements"])
    for change in changes:
        if not isinstance(change, dict) or not isinstance(change.get("id"), str) or not ID_PATTERN.fullmatch(change["id"]):
            raise Problem("The whiteboard contains an invalid object ID.")
        key = change["id"]
        if change.get("element") is None:
            elements.pop(key, None)
        else:
            elements[key] = clean_element(change["element"], key, lesson["_id"])
    if len(elements) > MAX_OBJECTS or len(json.dumps(elements)) > 6 * 1024 * 1024:
        raise Problem("This board is full. Remove unused objects before adding more.")
    result = db().whiteboards.update_one({"_id": lesson["_id"], "revision": board["revision"]}, {
        "$set": {"elements": elements, "updated_at": now(), "updated_by": g.user["_id"]},
        "$inc": {"revision": 1},
    })
    if not result.matched_count:
        return jsonify(snapshot(load_board(lesson["_id"]))), 409
    return {"revision": board["revision"] + 1}


def live_snapshot(lesson_id, client_id=None):
    timestamp = now()
    rows = db().whiteboard_live.find({
        "lesson_id": lesson_id, "seen_at": {"$gt": timestamp - timedelta(seconds=PRESENCE_SECONDS)},
        "left": False,
    })
    viewers, previews = {}, []
    for row in rows:
        user_id = str(row["user_id"])
        viewers[user_id] = {"id": user_id, "name": row["name"], "role": row["role"]}
        own_client = row["user_id"] == g.user["_id"] and row["client_id"] == client_id
        if (not own_client and row.get("elements") and
                row.get("preview_at", row["seen_at"]) > timestamp - timedelta(seconds=PREVIEW_SECONDS)):
            previews.append({"client_id": row["client_id"], "user_id": user_id, "elements": row["elements"]})
    # Read the revision after previews: a committed draft can be cleared while
    # this request runs, and the response must include its saved revision.
    board = db().whiteboards.find_one({"_id": lesson_id}, {"revision": 1})
    return {"revision": board["revision"] if board else 0,
            "viewers": sorted(viewers.values(), key=lambda viewer: (viewer["name"].casefold(), viewer["id"])),
            "previews": previews}


def live_client_id(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise Problem("Reload the whiteboard to reconnect.")
    return value


@bp.get("/lessons/<lesson_id>/whiteboard/live")
@requires("tutor", "parent", "student")
def live_state(lesson_id):
    lesson = accessible_lesson(lesson_id)
    client_id = request.args.get("client_id")
    if client_id is not None:
        live_client_id(client_id)
    return live_snapshot(lesson["_id"], client_id)


@bp.post("/lessons/<lesson_id>/whiteboard/live")
@requires("tutor", "parent", "student")
def live_update(lesson_id):
    lesson = accessible_lesson(lesson_id)
    if request.content_length and request.content_length > 2 * 1024 * 1024:
        raise Problem("Share smaller groups of whiteboard changes.", 413)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise Problem("Reload the whiteboard to reconnect.")
    client_id = live_client_id(payload.get("client_id"))
    sequence = payload.get("sequence")
    if type(sequence) is not int or not 0 <= sequence <= 2 ** 53 - 1:
        raise Problem("Reload the whiteboard to reconnect.")
    if "leave" in payload and type(payload["leave"]) is not bool:
        raise Problem("Reload the whiteboard to reconnect.")
    timestamp = now()
    fields = {"lesson_id": lesson["_id"], "user_id": g.user["_id"], "client_id": client_id,
              "name": g.user["name"], "role": g.user["role"], "sequence": sequence,
              "seen_at": timestamp, "expires_at": timestamp + timedelta(seconds=PRESENCE_SECONDS),
              "left": payload.get("leave", False)}
    if "elements" in payload:
        elements = payload["elements"]
        if not isinstance(elements, list) or len(elements) > 200:
            raise Problem("Share up to 200 whiteboard objects at a time.")
        cleaned, ids = [], set()
        for element in elements:
            key = element.get("id") if isinstance(element, dict) else None
            if not isinstance(key, str) or not ID_PATTERN.fullmatch(key) or key in ids:
                raise Problem("The whiteboard contains an invalid object ID.")
            ids.add(key)
            cleaned.append(clean_element(element, key, lesson["_id"]))
        fields.update(elements=cleaned, preview_at=timestamp)
    if fields["left"]:
        fields["elements"] = []
    # The row is shared across web workers. Sequence numbers prevent delayed
    # preview requests from overwriting a newer clear/leave request.
    key = f"{lesson['_id']}:{g.user['_id']}:{client_id}"
    try:
        db().whiteboard_live.update_one({"_id": key, "sequence": {"$lt": sequence}},
                                       {"$set": fields}, upsert=True)
    except DuplicateKeyError:
        pass  # A newer update from this browser is already visible.
    return live_snapshot(lesson["_id"], client_id)


def pdf_pages(data):
    import pypdfium2 as pdfium
    pages = []
    try:
        with PDF_LOCK, pdfium.PdfDocument(data) as document:
            if not 1 <= len(document) <= 40:
                raise Problem("Upload a PDF with one to 40 pages. Split longer documents first.")
            total_bytes = 0
            for index in range(len(document)):
                page = document[index]
                try:
                    width, height = page.get_size()
                    if width <= 0 or height <= 0:
                        raise Problem("This PDF has an invalid page size.")
                    bitmap = page.render(scale=min(2, 1600 / max(width, height)))
                    try:
                        image = bitmap.to_pil().convert("RGB")
                        output = BytesIO()
                        image.save(output, "JPEG", quality=90)
                        contents = output.getvalue()
                        total_bytes += len(contents)
                        if total_bytes > 30 * 1024 * 1024:
                            raise Problem("The rendered PDF is too large. Split it into smaller documents.")
                        pages.append({"data": Binary(contents), "width": image.width, "height": image.height,
                                      "page": index + 1, "mimetype": "image/jpeg"})
                    finally:
                        bitmap.close()
                finally:
                    page.close()
    except (pdfium.PdfiumError, ValueError, OSError, RuntimeError):
        raise Problem("Upload a readable PDF without a password.") from None
    return pages


@bp.post("/lessons/<lesson_id>/whiteboard/assets")
@requires("tutor", "parent", "student")
def upload(lesson_id):
    lesson = accessible_lesson(lesson_id)
    file = request.files.get("file")
    if not file or not file.filename:
        raise Problem("Choose a PDF or image to add to the board.")
    filename = secure_filename(file.filename)[:180] or "resource"
    if filename.lower().endswith(".pdf"):
        data = file.stream.read(MAX_PHOTO_BYTES + 1)
        if len(data) > MAX_PHOTO_BYTES:
            raise Problem("Each PDF or image must be 8 MB or smaller.", 413)
        if not data.startswith(b"%PDF-"):
            raise Problem("Upload a readable PDF.")
        assets = pdf_pages(data)
    else:
        data = prepare_photo(file)
        with Image.open(BytesIO(data)) as image:
            assets = [{"data": data, "width": image.width, "height": image.height, "mimetype": "image/jpeg"}]
    for asset in assets:
        asset.update(_id=ObjectId(), lesson_id=lesson["_id"], filename=filename, created_at=now(), uploaded_by=g.user["_id"])
    load_board(lesson["_id"])

    def persist(transaction):
        # Serialize the quota check with other imports for the same lesson.
        db().whiteboards.update_one({"_id": lesson["_id"]}, {"$inc": {"upload_revision": 1}}, session=transaction)
        count = db().whiteboard_assets.count_documents({"lesson_id": lesson["_id"]}, session=transaction)
        if count + len(assets) > MAX_ASSETS:
            raise Problem("This lesson has reached its limit of 200 uploaded pages and images.")
        db().whiteboard_assets.insert_many(assets, session=transaction)

    atomic(persist)
    return {"assets": [{"id": str(asset["_id"]), "width": asset["width"], "height": asset["height"],
                        "filename": filename, "page": asset.get("page")} for asset in assets]}, 201


@bp.get("/lessons/<lesson_id>/whiteboard/assets/<asset_id>")
@requires("tutor", "parent", "student")
def asset(lesson_id, asset_id):
    lesson = accessible_lesson(lesson_id)
    resource = db().whiteboard_assets.find_one({"_id": object_id(asset_id), "lesson_id": lesson["_id"]})
    if not resource:
        abort(404)
    return send_file(BytesIO(resource["data"]), mimetype=resource["mimetype"], download_name="whiteboard-page.jpg")
