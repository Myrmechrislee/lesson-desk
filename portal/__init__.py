from datetime import timedelta
import os
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, g, redirect, render_template, request, session, url_for
from flask_session import Session
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from pymongo import MongoClient
from werkzeug.exceptions import HTTPException, SecurityError

from . import accounts, ai, announcements, billing, calendar_feed, calls, comments, homework, lessons, planning, whiteboard
from . import markdown as markdown_feature
from .core import Problem, day_label, db, money, now, object_id, stamp
from .reminders import setup_reminders


def create_app(config=None):
    load_dotenv()
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY"),
        MONGO_URI=os.getenv("MONGO_URI", "mongodb://localhost:27017/?replicaSet=rs0"),
        MONGO_DB=os.getenv("MONGO_DB", "lesson_desk"),
        APP_BASE_URL=os.getenv("APP_BASE_URL", "http://localhost:3001").rstrip("/"),
        APP_TIMEZONE=os.getenv("APP_TIMEZONE", "Asia/Shanghai"),
        AUTH0_DOMAIN=os.getenv("AUTH0_DOMAIN", ""),
        AUTH0_CLIENT_ID=os.getenv("AUTH0_CLIENT_ID", ""),
        AUTH0_CLIENT_SECRET=os.getenv("AUTH0_CLIENT_SECRET", ""),
        AUTH0_MANAGEMENT_CLIENT_ID=os.getenv("AUTH0_MANAGEMENT_CLIENT_ID", ""),
        AUTH0_MANAGEMENT_CLIENT_SECRET=os.getenv("AUTH0_MANAGEMENT_CLIENT_SECRET", ""),
        AUTH0_MANAGEMENT_AUDIENCE=os.getenv("AUTH0_MANAGEMENT_AUDIENCE", "").strip(),
        AUTH0_CONNECTION=os.getenv("AUTH0_CONNECTION", "Username-Password-Authentication"),
        SMTP_HOST=os.getenv("SMTP_HOST", ""),
        SMTP_PORT=int(os.getenv("SMTP_PORT", "587")),
        SMTP_SECURITY=os.getenv("SMTP_SECURITY", "starttls").strip().lower(),
        SMTP_USERNAME=os.getenv("SMTP_USERNAME", ""),
        SMTP_PASSWORD=os.getenv("SMTP_PASSWORD", ""),
        MAIL_FROM=os.getenv("MAIL_FROM", ""),
        OPENROUTER_API_KEY=os.getenv("OPENROUTER_API_KEY", ""),
        OPENROUTER_PLAN_MODEL=os.getenv("OPENROUTER_PLAN_MODEL", "google/gemini-2.5-flash-lite"),
        OPENROUTER_FEEDBACK_MODEL=os.getenv("OPENROUTER_FEEDBACK_MODEL", "google/gemini-2.5-flash"),
        CALL_ICE_SERVERS=os.getenv("CALL_ICE_SERVERS", '[{"urls":"stun:stun.l.google.com:19302"}]'),
        CALL_TURN_SECRET=os.getenv("CALL_TURN_SECRET", ""),
        CALL_TURN_URLS=os.getenv("CALL_TURN_URLS", ""),
        MAX_CONTENT_LENGTH=42 * 1024 * 1024,
        MAX_FORM_PARTS=64,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        SESSION_COOKIE_NAME="lesson_desk_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_REFRESH_EACH_REQUEST=False,
    )
    if config:
        app.config.update(config)
    app.config["SMTP_SECURITY"] = app.config["SMTP_SECURITY"].strip().lower()

    base = urlsplit(app.config["APP_BASE_URL"])
    if (
        base.scheme not in {"http", "https"} or not base.hostname
        or base.path or base.query or base.fragment or base.username
    ):
        raise RuntimeError("APP_BASE_URL must be the app origin, such as https://lessons.example.com.")
    if base.scheme != "https" and base.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("APP_BASE_URL must use HTTPS outside localhost.")
    domain = app.config["AUTH0_DOMAIN"]
    if not re.fullmatch(r"[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", domain):
        raise RuntimeError("Set AUTH0_DOMAIN to the Auth0 tenant hostname, without https://.")
    if not app.config["AUTH0_CLIENT_ID"] or not app.config["AUTH0_CLIENT_SECRET"]:
        raise RuntimeError("Set the Auth0 web application client ID and secret.")
    if len(app.secret_key or "") < 32:
        raise RuntimeError("Set SECRET_KEY to a random value of at least 32 characters.")
    if app.config["SMTP_SECURITY"] not in {"starttls", "ssl", "none"}:
        raise RuntimeError("SMTP_SECURITY must be starttls, ssl, or none.")

    local_hosts = ["localhost", "127.0.0.1"]
    is_local = base.hostname in local_hosts
    app.config.update(
        AUTH0_ORIGIN=f"https://{domain}",
        AUTH0_MANAGEMENT_AUDIENCE=app.config["AUTH0_MANAGEMENT_AUDIENCE"] or f"https://{domain}/api/v2/",
        SESSION_COOKIE_SECURE=base.scheme == "https",
        TRUSTED_HOSTS=local_hosts if is_local else [base.hostname],
    )
    app.extensions["zone"] = ZoneInfo(app.config["APP_TIMEZONE"])
    mongo = MongoClient(app.config["MONGO_URI"], tz_aware=True, serverSelectionTimeoutMS=5000)
    app.extensions["mongo"] = mongo
    app.extensions["db"] = mongo[app.config["MONGO_DB"]]
    app.config.update(
        SESSION_TYPE="mongodb",
        SESSION_MONGODB=mongo,
        SESSION_MONGODB_DB=app.config["MONGO_DB"],
        SESSION_MONGODB_COLLECT="sessions",
        SESSION_SERIALIZATION_FORMAT="msgpack",
    )
    Session(app)
    # The canonical origin and Secure/SameSite cookies also work behind an HTTPS proxy.
    # Flask-WTF still verifies the per-session form token on every mutating request.
    app.config["WTF_CSRF_SSL_STRICT"] = False

    accounts.configure_oidc(app)
    for feature in (accounts, announcements, lessons, homework, billing, calendar_feed, whiteboard, markdown_feature, comments, planning, ai, calls):
        app.register_blueprint(feature.bp)

    app.jinja_env.filters.update(stamp=stamp, day_label=day_label, money=money,
                                 markdown=markdown_feature.render_markdown)
    app.jinja_env.globals["zone_name"] = app.config["APP_TIMEZONE"]

    @app.context_processor
    def account_notices():
        user = getattr(g, "user", None)
        if not user:
            return {}
        return {"setup_reminders": setup_reminders(user), **announcements.notices_for(user)}

    @app.before_request
    def canonical_local_host():
        # Keep login state and the Auth0 callback on the same cookie hostname.
        if is_local and urlsplit(request.host_url).hostname != base.hostname:
            return redirect(app.config["APP_BASE_URL"] + request.full_path.removesuffix("?"), code=307)

    @app.before_request
    def load_account():
        g.user = None
        if request.endpoint not in {"static", "health"} and session.get("user_id"):
            if session.get("auth_expires_at", 0) <= now().timestamp():
                session.clear()
                return
            user_id = object_id(session["user_id"])
            g.user = db().users.find_one({"_id": user_id, "active": True})
            if not g.user:
                session.clear()

    CSRFProtect(app)

    @app.after_request
    def set_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; object-src 'none'; base-uri 'self'; "
            f"form-action 'self' {app.config['AUTH0_ORIGIN']}; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def home():
        if g.user:
            endpoint = {
                "admin": "accounts.index",
                "tutor": "lessons.index",
                "parent": "lessons.index",
                "student": "homework.index",
            }[g.user["role"]]
            return redirect(url_for(endpoint))
        return render_template("welcome.html")

    @app.get("/healthz")
    def health():
        db().command("ping")
        return {"status": "ok"}

    @app.errorhandler(Problem)
    def handle_problem(error):
        return render_template("error.html", message=error.message, status=error.status), error.status

    @app.errorhandler(HTTPException)
    def handle_http_error(error):
        # A rejected host has no URL adapter, so templates using url_for cannot render.
        if isinstance(error, SecurityError):
            return error.get_response()
        message = {
            403: "This page is unavailable for your account.",
            404: "We couldn't find that page.",
            413: "The upload is too large. Upload up to five files or photos of 8 MB each.",
        }.get(error.code, error.description)
        if isinstance(error, CSRFError):
            message = "This form expired. Open the page again and resubmit it."
        return render_template("error.html", message=message, status=error.code), error.code

    from .cli import register_commands

    register_commands(app)
    return app
