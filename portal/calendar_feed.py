"""Private, revocable iCalendar subscriptions for each account's lessons."""
from datetime import timedelta
import secrets

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, url_for
from itsdangerous import BadSignature, URLSafeSerializer

from .core import db, now, object_id, people, requires, today
from .recurring import HORIZON_WEEKS, maintain_series

bp = Blueprint("calendar", __name__)


def account_scope(user):
    if user["role"] == "parent":
        children = db().users.distinct("_id", {"parent_id": user["_id"], "role": "student", "active": True})
        return {"student_id": {"$in": children}}
    return {user["role"] + "_id": user["_id"]}


def serializer():
    return URLSafeSerializer(current_app.secret_key, salt="lesson-calendar-v1")


def subscription_url(user):
    version = user.get("calendar_version")
    if not version:
        return None
    token = serializer().dumps({"user": str(user["_id"]), "version": version})
    return current_app.config["APP_BASE_URL"] + url_for("calendar.feed", token=token)


@bp.get("/calendar")
@requires("tutor", "parent", "student")
def index():
    link = subscription_url(g.user)
    return render_template("calendar.html", calendar_url=link,
                           webcal_url=link.replace("https://", "webcal://", 1).replace("http://", "webcal://", 1) if link else None)


@bp.post("/calendar/renew")
@requires("tutor", "parent", "student")
def renew():
    db().users.update_one({"_id": g.user["_id"], "active": True}, {"$set": {"calendar_version": secrets.token_urlsafe(32)}})
    flash("Your calendar link is ready. Previous subscription links have been revoked.")
    return redirect(url_for("calendar.index"), code=303)


@bp.post("/calendar/revoke")
@requires("tutor", "parent", "student")
def revoke():
    db().users.update_one({"_id": g.user["_id"]}, {"$unset": {"calendar_version": ""}})
    flash("Calendar subscription revoked.")
    return redirect(url_for("calendar.index"), code=303)


def ical_text(value):
    return str(value).replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def fold_line(line):
    """RFC 5545 lines are at most 75 octets, including continuation spaces."""
    parts, current = [], ""
    for character in line:
        if len((current + character).encode("utf-8")) > 75:
            parts.append(current)
            current = " "
        current += character
    parts.append(current)
    return "\r\n".join(parts)


def calendar_document(lessons, names, *, timestamp=None):
    timestamp = timestamp or now()
    date = lambda value: value.strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Lesson Desk//Lessons//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:Lesson Desk", "REFRESH-INTERVAL;VALUE=DURATION:PT1H"]
    for lesson in lessons:
        student = names.get(lesson["student_id"], {}).get("name", "Student")
        tutor = names.get(lesson["tutor_id"], {}).get("name", "Tutor")
        updated = lesson.get("updated_at") or lesson.get("finalized_at") or lesson.get("created_at") or lesson["_id"].generation_time
        url = current_app.config["APP_BASE_URL"] + url_for("lessons.detail", lesson_id=lesson["_id"])
        lines += ["BEGIN:VEVENT", f"UID:lesson-{lesson['_id']}@lesson-desk", "DTSTAMP:" + date(timestamp),
                  "LAST-MODIFIED:" + date(updated), "DTSTART:" + date(lesson["start_at"]), "DTEND:" + date(lesson["end_at"]),
                  "SUMMARY:" + ical_text(f"{lesson.get('topic') or lesson.get('title') or 'Lesson'} · {student}"),
                  "DESCRIPTION:" + ical_text(f"Student: {student}\nTutor: {tutor}\nOpen Lesson Desk for lesson details."),
                  "URL:" + url, "STATUS:" + ("CANCELLED" if lesson["status"] in {"cancelled", "cancelled_late"} else "CONFIRMED"),
                  "CLASS:PRIVATE", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_line(line) for line in lines) + "\r\n"


@bp.get("/calendar/feed/<token>.ics")
def feed(token):
    if len(token) > 1000:
        abort(404)
    try:
        payload = serializer().loads(token)
    except BadSignature:
        abort(404)
    if not isinstance(payload, dict) or not isinstance(payload.get("user"), str) or not isinstance(payload.get("version"), str):
        abort(404)
    user = db().users.find_one({"_id": object_id(payload["user"]), "active": True,
                                "role": {"$in": ["tutor", "parent", "student"]}, "calendar_version": payload["version"]})
    if not user:
        abort(404)
    scope = account_scope(user)
    maintain_series(until=today() + timedelta(weeks=HORIZON_WEEKS), scope=scope)
    lessons = list(db().lessons.find({**scope, "end_at": {"$gte": now() - timedelta(days=90)}}).sort("start_at", 1))
    names = people(key for lesson in lessons for key in (lesson["student_id"], lesson["tutor_id"]))
    return Response(calendar_document(lessons, names), content_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'inline; filename="lesson-desk.ics"'})
