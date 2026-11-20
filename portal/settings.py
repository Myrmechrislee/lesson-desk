"""Account preferences shared by all date displays and scheduling forms."""
from zoneinfo import available_timezones

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .core import Problem, ROLES, db, preferences, requires, zone

bp = Blueprint("settings", __name__)


def timezone_field(form):
    name = form.get("timezone", "")
    if name not in available_timezones():
        raise Problem("Choose a valid timezone.")
    return name


@bp.route("/settings", methods=["GET", "POST"])
@requires(*ROLES)
def index():
    if request.method == "POST":
        name = timezone_field(request.form)
        time_format = request.form.get("time_format")
        if time_format not in {"12", "24"}:
            raise Problem("Choose a 12-hour or 24-hour clock.")
        db().users.update_one({"_id": g.user["_id"], "active": True}, {"$set": {
            "preferences.timezone": name, "preferences.time_format": time_format,
        }})
        flash("Your preferences have been saved.")
        return redirect(url_for("settings.index"), code=303)
    return render_template("settings.html", timezones=sorted(available_timezones()),
                           selected_timezone=zone().key,
                           selected_time_format=preferences().get("time_format", "12"))


@bp.post("/settings/detect-timezone")
@requires(*ROLES)
def detect_timezone():
    name = timezone_field(request.form)
    # Only initialize a missing preference; a stale tab cannot overwrite settings.
    result = db().users.update_one({
        "_id": g.user["_id"], "active": True, "preferences.timezone": {"$exists": False},
    }, {"$set": {"preferences.timezone": name}})
    return {"updated": bool(result.modified_count)}
