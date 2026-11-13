from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from io import BytesIO
import json
import os
import re
import socket
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from aiosmtpd.controller import Controller
from bson import ObjectId
from joserfc import jwt
from joserfc.jwk import RSAKey
from PIL import Image
import pytest
import requests

from portal import create_app
from portal.accounts import Auth0Management
from portal.billing import deliver_next, refresh_overdue
from portal.core import boundary, ensure_indexes, now, stamp, today


@pytest.fixture
def app(request):
    uri = os.environ.get("TEST_MONGO_URI")
    if not uri:
        pytest.skip("Set TEST_MONGO_URI to a disposable MongoDB replica set.")
    overrides = getattr(request, "param", {})
    if isinstance(overrides, str):
        overrides = {"APP_BASE_URL": overrides}
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-key-" * 6,
        "AUTH0_DOMAIN": "unit-test.auth0.com",
        "AUTH0_CLIENT_ID": "test-client",
        "AUTH0_CLIENT_SECRET": "test-client-secret",
        "AUTH0_MANAGEMENT_CLIENT_ID": "test-management",
        "AUTH0_MANAGEMENT_CLIENT_SECRET": "test-management-secret",
        "AUTH0_MANAGEMENT_AUDIENCE": "",
        "APP_TIMEZONE": "Asia/Shanghai",
        "APP_BASE_URL": "http://localhost:3001",
        "SMTP_SECURITY": "none",
        "SMTP_USERNAME": "",
        "SMTP_PASSWORD": "",
        "MONGO_URI": uri,
        "MONGO_DB": "lesson_test_" + uuid4().hex,
        **overrides,
    })
    with app.app_context():
        ensure_indexes()
    try:
        yield app
    finally:
        mongo = app.extensions["mongo"]
        mongo.drop_database(app.config["MONGO_DB"])
        mongo.close()


@pytest.fixture
def database(app):
    return app.extensions["db"]


@pytest.fixture
def users(database):
    accounts = {}
    for key, role in (
        ("admin", "admin"), ("tutor", "tutor"), ("parent", "parent"),
        ("student", "student"), ("sibling", "student"), ("other_parent", "parent"),
        ("other_student", "student"), ("other_tutor", "tutor"),
    ):
        account = {
            "_id": ObjectId(), "name": key.replace("_", " ").title(), "role": role, "active": True,
            "auth0_sub": f"auth0|{key}", "email": f"{key}@example.test",
        }
        if key in {"student", "sibling"}:
            account["parent_id"] = accounts["parent"]["_id"]
        if key == "other_student":
            account["parent_id"] = accounts["other_parent"]["_id"]
        if key == "tutor":
            account["billing"] = {
                "name": "Studio Tutoring", "email": "billing@example.test", "abn": "10000000000",
                "address": "Sydney, NSW", "phone": "", "payment": "Pay by bank transfer.", "due_days": 14,
            }
        database.users.insert_one(account)
        accounts[key] = account

    for key in ("student", "sibling", "other_student"):
        database.enrollments.insert_one({
            "tutor_id": accounts["tutor"]["_id"], "student_id": accounts[key]["_id"],
            "rate_cents": 6500, "weekly_target": 2,
        })
    database.weekly_availability.insert_many([
        {"tutor_id": accounts["tutor"]["_id"], "weekday": weekday, "start_min": 0, "end_min": 1440}
        for weekday in range(7)
    ])
    database.weekly_availability.insert_many([
        {"parent_id": accounts["parent"]["_id"], "weekday": weekday, "start_min": 0, "end_min": 1440}
        for weekday in range(7)
    ])
    return accounts


def sign_in(app, account):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = str(account["_id"])
        session["auth_expires_at"] = int((now() + timedelta(hours=1)).timestamp())
    return client


def csrf(client):
    response = client.get("/", follow_redirects=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))[1]


def post(client, path, **data):
    return client.post(path, data={"csrf_token": csrf(client), **data})


def lesson_record(database, users, student, start, **overrides):
    lesson = {
        "_id": ObjectId(), "title": f"Lesson for {student}", "tutor_id": users["tutor"]["_id"],
        "student_id": users[student]["_id"], "status": "scheduled", "invoice_id": None,
        "rate_cents": 6500, "start_at": start, "end_at": start + timedelta(hours=1),
        **overrides,
    }
    database.lessons.insert_one(lesson)
    return lesson


@pytest.mark.parametrize("app, alias", [
    ("http://localhost:3001", "http://127.0.0.1:3001"),
    ("http://127.0.0.1:3001", "http://localhost:3001"),
], indirect=["app"])
def test_local_alias_redirects_to_origin_before_login_and_csrf(app, alias):
    client = app.test_client()
    origin = app.config["APP_BASE_URL"]
    response = client.get("/", base_url=alias)
    assert response.status_code == 307
    assert response.location == origin + "/"
    assert client.get(response.location).status_code == 200

    response = client.get("/login?next=%2Flessons", base_url=alias)
    assert response.status_code == 307
    assert response.location == origin + "/login?next=%2Flessons"

    response = client.post("/logout", base_url=alias)
    assert response.status_code == 307
    assert response.location == origin + "/logout"
    assert client.post(response.location).status_code == 400


@pytest.mark.parametrize("app", [
    "http://localhost:3001", "https://lessons.example.test",
], indirect=True)
def test_untrusted_host_returns_bad_request_without_template_error(app):
    client = app.test_client()
    for path in ("/", "/favicon.ico", "/healthz"):
        response = client.get(path, base_url="https://untrusted.example")
        assert response.status_code == 400
        assert b"Bad Request" in response.data
    assert client.get("/", base_url=app.config["APP_BASE_URL"]).status_code == 200


@pytest.mark.parametrize("app", ["https://lessons.example.test"], indirect=True)
def test_production_origin_does_not_trust_local_aliases(app):
    client = app.test_client()
    for origin in ("http://localhost:3001", "http://127.0.0.1:3001"):
        assert client.get("/", base_url=origin).status_code == 400


def test_auth0_login_checks_pkce_state_nonce_and_subject(app, users, monkeypatch):
    key = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
    discovery = "https://unit-test.auth0.com"
    claims = {
        "iss": discovery + "/", "aud": "test-client", "sub": users["student"]["auth0_sub"],
        "iat": int(now().timestamp()), "exp": int(now().timestamp()) + 120, "nonce": "",
    }
    exchanged = []

    def endpoint(session, method, url, **kwargs):
        if url.endswith("/.well-known/openid-configuration"):
            payload = {
                "issuer": discovery + "/", "authorization_endpoint": discovery + "/authorize",
                "token_endpoint": discovery + "/oauth/token", "jwks_uri": discovery + "/.well-known/jwks.json",
                "id_token_signing_alg_values_supported": ["RS256"],
            }
        elif url.endswith("/jwks.json"):
            payload = {"keys": [key.as_dict(private=False)]}
        else:
            assert url == discovery + "/oauth/token"
            data = kwargs["data"]
            exchanged.append(parse_qs(data) if isinstance(data, str) else data)
            payload = {
                "access_token": "unused", "token_type": "Bearer",
                "id_token": jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, key),
            }
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", endpoint)
    client = app.test_client()

    def start_login():
        response = client.get("/login")
        query = parse_qs(urlsplit(response.location).query)
        assert query["code_challenge_method"] == ["S256"]
        assert query["connection"] == [app.config["AUTH0_CONNECTION"]]
        assert query["redirect_uri"] == ["http://localhost:3001/auth/callback"]
        assert query["prompt"] == ["login"]
        return query

    query = start_login()
    assert client.get("/auth/callback?code=first&state=incorrect").status_code == 401
    assert not exchanged
    assert client.get("/auth/callback", query_string={"code": "first", "state": query["state"][0]}).status_code == 401
    assert "code_verifier" in exchanged[-1]

    query = start_login()
    claims.update(nonce=query["nonce"][0], aud="other-client", azp="test-client")
    assert client.get("/auth/callback", query_string={"code": "wrong-audience", "state": query["state"][0]}).status_code == 401

    query = start_login()
    claims.update(nonce=query["nonce"][0], aud="test-client", sub="auth0|unprovisioned", email=users["student"]["email"])
    assert client.get("/auth/callback", query_string={"code": "second", "state": query["state"][0]}).status_code == 403

    query = start_login()
    claims.update(nonce=query["nonce"][0], sub=users["student"]["auth0_sub"])
    before_login = client.get_cookie(app.config["SESSION_COOKIE_NAME"]).value
    response = client.get("/auth/callback", query_string={"code": "third", "state": query["state"][0]})
    assert response.status_code == 302
    assert client.get_cookie(app.config["SESSION_COOKIE_NAME"]).value != before_login
    page = client.get("/homework")
    assert page.status_code == 200
    assert client.get("/admin/accounts").status_code == 403
    assert client.post("/logout").status_code == 400
    student_cookie = client.get_cookie(app.config["SESSION_COOKIE_NAME"]).value
    response = post(client, "/logout")
    logout_url = urlsplit(response.location)
    assert logout_url.path == "/oidc/logout"
    assert parse_qs(logout_url.query)["post_logout_redirect_uri"] == ["http://localhost:3001/"]
    # Browsers can enforce form-action on redirects after the sign-out POST.
    form_action = next(
        directive.split()[1:] for directive in page.headers["Content-Security-Policy"].split(";")
        if directive.strip().startswith("form-action ")
    )
    assert form_action == ["'self'", f"{logout_url.scheme}://{logout_url.netloc}"]
    with client.session_transaction() as session:
        assert "user_id" not in session
        assert "auth_expires_at" not in session

    stale_client = app.test_client()
    stale_client.set_cookie(app.config["SESSION_COOKIE_NAME"], student_cookie)
    assert urlsplit(stale_client.get("/homework").location).path == "/login"

    query = start_login()
    claims.update(nonce=query["nonce"][0], sub=users["tutor"]["auth0_sub"])
    response = client.get("/auth/callback", query_string={"code": "fourth", "state": query["state"][0]})
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session["user_id"] == str(users["tutor"]["_id"])
    assert urlsplit(client.get("/").location).path == "/lessons"
    assert client.get("/lessons").status_code == 200


@pytest.mark.parametrize("app", [
    {},
    {
        "AUTH0_DOMAIN": "login.example.test",
        "AUTH0_MANAGEMENT_AUDIENCE": "https://unit-test.auth0.com/api/v2/",
    },
], indirect=True)
def test_management_audience_with_default_and_custom_login_domains(app, monkeypatch):
    calls = []
    origin = app.config["AUTH0_ORIGIN"]

    def api(method, url, **kwargs):
        calls.append(url)
        if url == origin + "/oauth/token":
            assert method == "POST"
            assert kwargs["json"]["audience"] == "https://unit-test.auth0.com/api/v2/"
            payload = {"access_token": "test-management-token"}
        else:
            assert method == "GET" and url == origin + "/api/v2/users-by-email"
            assert kwargs["headers"]["Authorization"] == "Bearer test-management-token"
            payload = []
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(requests, "request", api)
    with app.app_context():
        management = Auth0Management()
        management.authorize()
        assert management.owned_subject({"email": "missing@example.test"}) is None
    assert calls == [origin + "/oauth/token", origin + "/api/v2/users-by-email"]


def test_demo_provisioning_and_admin_role_assignment(app, database, monkeypatch):
    auth0_users = {}
    calls = []

    def api(method, url, **kwargs):
        path = urlsplit(url).path
        calls.append(path)
        if path == "/oauth/token":
            payload = {"access_token": "test-management-token"}
        elif path == "/api/v2/users-by-email":
            user = auth0_users.get(kwargs["params"]["email"])
            payload = [user] if user else []
        elif path == "/api/v2/users" and method == "GET":
            username = json.loads(kwargs["params"]["q"].split(" AND identities.connection:")[0].removeprefix("username:"))
            user = auth0_users.get(username)
            payload = [user] if user else []
        elif path == "/api/v2/tickets/password-change":
            payload = {"ticket": "https://unit-test.auth0.com/setup?ticket=one-use"}
        else:
            assert path == "/api/v2/users" and method == "POST"
            body = kwargs["json"]
            assert body.get("verify_email", False) is False
            payload = {
                "user_id": f"auth0|{uuid4().hex}",
                "identities": [{"connection": body["connection"]}],
                "app_metadata": body["app_metadata"],
                "username": body.get("username"),
            }
            auth0_users[body.get("username") or body["email"]] = payload
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(requests, "request", api)
    monkeypatch.setenv("DEMO_PASSWORD", "DemoSecret-OnlyForTests7!")
    runner = app.test_cli_runner()
    for _ in range(2):
        result = runner.invoke(args=["seed-demo"])
        assert result.exit_code == 0, result.output
        assert "demo-student" in result.output
    assert len(auth0_users) == 4
    assert database.users.count_documents({"active": True}) == 4
    assert database.lessons.count_documents({}) == 2
    assert database.homework.count_documents({}) == 1

    admin = sign_in(app, database.users.find_one({"role": "admin"}))
    response = post(admin, "/admin/accounts", name="New person", username="new-student", email="new@example.test", role="student")
    assert response.status_code == 201
    assert b"Password setup link" in response.data
    new_account = database.users.find_one({"email": "new@example.test"})
    assert "password" not in new_account
    assert post(admin, f"/admin/accounts/{new_account['_id']}/role", role="tutor").status_code == 303
    assert database.users.find_one({"_id": new_account["_id"]})["role"] == "tutor"
    assert admin.get("/admin/students").status_code == 200


def test_parent_privacy_and_multiple_children(app, database, users):
    start = now() + timedelta(days=2)
    first = lesson_record(database, users, "student", start)
    second = lesson_record(database, users, "sibling", start + timedelta(hours=2))
    foreign = lesson_record(database, users, "other_student", start + timedelta(hours=4))
    past = lesson_record(database, users, "student", now() - timedelta(hours=4), status="completed",
                         title="Past private lesson", feedback="Private lesson feedback")
    parent = sign_in(app, users["parent"])
    page = parent.get("/lessons")
    assert page.status_code == 200
    assert first["title"].encode() in page.data and second["title"].encode() in page.data
    assert foreign["title"].encode() not in page.data
    assert b"Past private lesson" not in page.data and b"Private lesson feedback" not in page.data
    filtered = parent.get("/lessons", query_string={"child": users["sibling"]["_id"]})
    assert second["title"].encode() in filtered.data and first["title"].encode() not in filtered.data
    assert parent.get(f"/lessons/{foreign['_id']}/change").status_code == 404
    feedback_page = parent.get(f"/lessons/{past['_id']}")
    assert feedback_page.status_code == 200
    assert b"Private lesson feedback" not in feedback_page.data
    assert parent.get("/homework").status_code == 200
    assert b"Past private lesson" in parent.get("/lessons?view=past").data
    assert sign_in(app, users["student"]).get(f"/lessons/{past['_id']}").status_code == 200
    assert sign_in(app, users["sibling"]).get(f"/lessons/{past['_id']}").status_code == 404


def test_repeating_schedule_uses_exceptions_and_prevents_concurrent_overlap(app, database, users):
    tutor = sign_in(app, users["tutor"])
    with app.app_context():
        day = today() + timedelta(days=3)
        first = boundary(day, 840)
        repeat = boundary(day + timedelta(days=7), 840)
        unavailable = stamp(repeat, "input"), stamp(repeat + timedelta(hours=1), "input")
        starts_at = stamp(first, "input")

    assert post(tutor, "/availability/exception", kind="unavailable", starts_at=unavailable[0],
                ends_at=unavailable[1]).status_code == 303
    form = {"student_id": str(users["student"]["_id"]), "starts_at": starts_at,
            "title": "Weekly class", "minutes": "60", "weeks": "2"}
    assert post(tutor, "/lessons/new", **form).status_code == 409
    assert database.lessons.count_documents({}) == 0

    def book(student):
        client = sign_in(app, users["tutor"])
        return post(client, "/lessons/new", **{**form, "student_id": str(users[student]["_id"]), "weeks": "1"}).status_code

    with ThreadPoolExecutor(max_workers=2) as threads:
        assert sorted(threads.map(book, ("student", "sibling"))) == [303, 409]
    assert database.lessons.count_documents({}) == 1

    with app.app_context():
        extra_start = stamp(first + timedelta(hours=2), "input")
        extra_end = stamp(first + timedelta(hours=3), "input")
    database.weekly_availability.delete_many({})
    assert post(tutor, "/availability/exception", kind="available", starts_at=extra_start, ends_at=extra_end).status_code == 303
    parent = sign_in(app, users["parent"])
    assert post(parent, "/availability/exception", kind="available", starts_at=extra_start, ends_at=extra_end).status_code == 303
    assert post(tutor, "/lessons/new", **{**form, "starts_at": extra_start, "weeks": "1"}).status_code == 303


def test_late_change_acknowledgment_and_discretion(app, database, users):
    parent = sign_in(app, users["parent"])
    tutor = sign_in(app, users["tutor"])
    original = lesson_record(database, users, "student", now() + timedelta(hours=2))
    with app.app_context():
        desired = stamp(boundary(today() + timedelta(days=2), 960), "input")
    path = f"/lessons/{original['_id']}/change"
    page = parent.get(path)
    assert b"need to pay the full price" in page.data
    assert post(parent, path, starts_at=desired, reason="Family appointment").status_code == 400
    assert post(parent, path, starts_at=desired, reason="Family appointment", acknowledge_late="yes").status_code == 303
    change = database.change_requests.find_one({"lesson_id": original["_id"]})
    assert change["late"] and change["reason"] == "Family appointment"
    review = f"/changes/{change['_id']}"
    assert post(parent, review, action="approve").status_code == 403
    assert post(tutor, review, action="approve", charge_original="yes").status_code == 400
    assert post(tutor, review, action="approve", charge_original="yes", parent_feedback="No lesson was held.").status_code == 303
    billed = database.lessons.find_one({"_id": original["_id"]})
    assert billed["status"] == "cancelled_late" and billed["charge"]
    assert database.lessons.find_one({"_id": billed["replaced_by"]})["status"] == "scheduled"

    other = lesson_record(database, users, "sibling", now() + timedelta(hours=4))
    with app.app_context():
        desired = stamp(boundary(today() + timedelta(days=3), 900), "input")
    assert post(tutor, f"/lessons/{other['_id']}/change", starts_at=desired).status_code == 303
    proposal = database.change_requests.find_one({"lesson_id": other["_id"]})
    assert post(parent, f"/changes/{proposal['_id']}", action="approve").status_code == 303
    moved = database.lessons.find_one({"_id": other["_id"]})
    assert moved["status"] == "scheduled" and moved["start_at"] == proposal["start_at"]


def test_homework_photo_upload_pushes_to_tutor(app, database, users):
    tutor = sign_in(app, users["tutor"])
    student = sign_in(app, users["student"])
    assert post(tutor, "/homework/new", title="Fractions", instructions="Show your work",
                student_id=str(users["student"]["_id"])).status_code == 303
    homework_id = database.homework.find_one({})["_id"]
    path = f"/homework/{homework_id}"
    assert student.get(path).status_code == 200
    assert sign_in(app, users["sibling"]).get(path).status_code == 404
    assert sign_in(app, users["other_tutor"]).get(path + "/live").status_code == 404

    response = tutor.get(path + "/live", buffered=False)
    try:
        chunks = iter(response.response)
        assert b"retry:" in next(chunks)
        assert b"data: []" in next(chunks)
        photo = BytesIO()
        Image.new("RGB", (32, 32), "green").save(photo, format="PNG")
        photo.seek(0)
        assert post(student, path + "/photos", photos=(photo, "work.png")).status_code == 303
        item = database.homework_photos.find_one({"homework_id": homework_id})
        assert str(item["_id"]).encode() in next(chunks)
    finally:
        response.close()

    response = tutor.get(f"{path}/photos/{item['_id']}")
    assert response.mimetype == "image/jpeg" and response.data.startswith(b"\xff\xd8")
    assert sign_in(app, users["parent"]).get(f"{path}/photos/{item['_id']}").status_code == 200
    assert sign_in(app, users["other_parent"]).get(f"{path}/photos/{item['_id']}").status_code == 404


def test_outcomes_invoice_delivery_deduplication_and_overdue(app, database, users):
    tutor = sign_in(app, users["tutor"])
    parent = sign_in(app, users["parent"])
    first = lesson_record(database, users, "student", now() - timedelta(days=2))
    second = lesson_record(database, users, "sibling", now() - timedelta(days=1))
    outcome = f"/lessons/{first['_id']}/outcome"
    assert post(tutor, outcome, status="completed", parent_feedback=" ").status_code == 400
    assert post(tutor, outcome, status="completed", parent_feedback="Private lesson feedback").status_code == 303
    assert post(tutor, f"/lessons/{second['_id']}/outcome", status="cancelled", parent_feedback="Private note",
                reason="Private cancellation reason").status_code == 303

    def generate(_):
        client = sign_in(app, users["tutor"])
        return post(client, "/invoices", parent_id=str(users["parent"]["_id"])).status_code

    with ThreadPoolExecutor(max_workers=2) as threads:
        assert sorted(threads.map(generate, range(2))) == [303, 409]

    invoice = database.invoices.find_one({})
    assert invoice["total_cents"] == 6500
    assert len(invoice["lines"]) == 2
    assert all(set(line) == {"lesson_id", "student", "start_at", "end_at", "amount_cents"} for line in invoice["lines"])
    assert invoice["pdf"].startswith(b"%PDF")
    assert post(tutor, outcome, status="completed", parent_feedback="Edited").status_code == 409

    pdf_url = f"/invoices/{invoice['_id']}/pdf"
    assert parent.get(pdf_url).data == invoice["pdf"]
    assert sign_in(app, users["other_parent"]).get(pdf_url).status_code == 404
    assert sign_in(app, users["other_tutor"]).get(pdf_url).status_code == 404
    assert post(parent, f"/invoices/{invoice['_id']}/status", status="paid").status_code == 403

    messages = []

    class Receiver:
        async def handle_DATA(self, server, session, envelope):
            messages.append(envelope.original_content)
            return "250 OK"

    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        port = connection.getsockname()[1]
    smtp = Controller(Receiver(), hostname="127.0.0.1", port=port)
    smtp.start()
    try:
        app.config.update(SMTP_HOST="127.0.0.1", SMTP_PORT=port, SMTP_SECURITY="none", MAIL_FROM="billing@example.test")
        with app.app_context():
            assert deliver_next()
            assert not deliver_next()
    finally:
        smtp.stop()
    assert len(messages) == 1
    message = BytesParser(policy=policy.default).parsebytes(messages[0])
    assert message["To"] == users["parent"]["email"]
    assert next(message.iter_attachments()).get_payload(decode=True) == invoice["pdf"]
    assert database.invoices.find_one({})["delivery"]["state"] == "sent"

    database.invoices.update_one({"_id": invoice["_id"]}, {"$set": {"due_at": now() - timedelta(seconds=1)}})
    with app.app_context():
        assert refresh_overdue() == 1
    assert database.invoices.find_one({})["status"] == "overdue"
    assert post(tutor, f"/invoices/{invoice['_id']}/status", status="paid").status_code == 303
    assert database.invoices.find_one({})["status"] == "paid"
    with app.app_context():
        assert refresh_overdue() == 0
