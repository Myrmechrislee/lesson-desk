from datetime import timedelta
import smtplib

from bson import ObjectId
from flask import Flask
import pytest

from portal.accounts import Auth0Management, ManagementError, provision
from portal.core import Problem, now
from portal.onboarding import deliver_next_onboarding, enqueue_onboarding, send_onboarding
from test_portal import app, database, post, sign_in, users


@pytest.fixture
def relay(monkeypatch):
    sent = []

    class Relay:
        def __init__(self, *args, **kwargs):
            pass

        def starttls(self, **kwargs):
            pass

        def login(self, username, password):
            pass

        def send_message(self, message, **kwargs):
            sent.append((message, kwargs))

        def close(self):
            pass

    monkeypatch.setattr("portal.onboarding.smtplib.SMTP", Relay)
    monkeypatch.setattr("portal.onboarding.smtplib.SMTP_SSL", Relay)
    return sent


@pytest.mark.parametrize("security", ["none", "starttls", "ssl"])
def test_onboarding_email_contains_guide_private_links_and_bold_fee(security, relay, monkeypatch):
    application = Flask("portal")
    application.config.update(
        MAIL_FROM="Lesson Desk <hello@example.test>", APP_BASE_URL="https://lessons.example.test",
        AUTH0_DOMAIN="unit-test.auth0.com", SMTP_HOST="smtp.example.test", SMTP_PORT=587,
        SMTP_SECURITY=security, SMTP_USERNAME="relay-user", SMTP_PASSWORD="test-password",
    )
    monkeypatch.setattr("portal.onboarding.validate_recipients", lambda contents: None)
    contents = {
        "parent": {"name": "Parent <script>alert(1)</script>", "email": "parent@example.test"},
        "students": [
            {"name": "First & Child", "username": "first.kid", "setup_url": "https://unit-test.auth0.com/setup?ticket=first&kind=child"},
            {"name": "Second Child", "username": "second.kid", "setup_url": "https://unit-test.auth0.com/setup?ticket=second"},
        ],
        "setup_url": "https://unit-test.auth0.com/setup?ticket=parent&kind=adult", "student_access_only": False,
        "links": {name: "https://lessons.example.test/" + path for name, path in {
            "login": "login", "availability": "availability", "lessons": "lessons",
            "course_plans": "course-plans", "homework": "homework", "invoices": "invoices",
        }.items()},
    }
    with application.app_context():
        send_onboarding({"_id": "welcome:test", "attempts": 1}, contents)
    message, envelope = relay[0]
    assert message["Subject"] == "Welcome to Lesson Desk"
    assert str(message["From"]) == str(message["Reply-To"]) == application.config["MAIL_FROM"]
    assert envelope == {"from_addr": "hello@example.test", "to_addrs": ["parent@example.test"]}
    assert not list(message.iter_attachments())
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    for body in (plain, html):
        for value in ("first.kid", "second.kid", "24 hours", "Availability", "Open whiteboard", "Join call",
                      "Request change", "Cancel lesson", "Homework", "Invoices"):
            assert value in body
        assert all(url in body for url in contents["links"].values())
    assert contents["setup_url"] in plain
    assert "&kind=child" in plain and "&amp;kind=child" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<strong>Cancellations made less than 24 hours" in html
    assert "at least 24 hours before the start are free.</strong>" in html


def configure_mail(app):
    app.config.update(SMTP_HOST="smtp.example.test", MAIL_FROM="hello@example.test")


def setup_provider(monkeypatch):
    calls = []
    monkeypatch.setattr(Auth0Management, "create_subject", lambda self, account, password: "auth0|" + str(account["_id"]))

    def setup_link(self, subject):
        calls.append(subject)
        return "https://unit-test.auth0.com/setup?ticket=" + subject.removeprefix("auth0|")

    monkeypatch.setattr(Auth0Management, "setup_link", setup_link)
    return calls


def test_new_parent_queues_once_and_welcome_includes_all_current_children(app, database, users, relay, monkeypatch):
    configure_mail(app)
    calls = setup_provider(monkeypatch)
    admin = sign_in(app, users["admin"])
    assert post(admin, "/admin/accounts", name="New Parent", email="new-parent@example.test", role="parent").status_code == 303
    parent = database.users.find_one({"email": "new-parent@example.test"})
    assert parent["active"] and calls == []
    for key, username in (("student", "first.kid"), ("sibling", "second.kid")):
        database.users.update_one({"_id": users[key]["_id"]}, {"$set": {"username": username}, "$unset": {"email": ""}})
        assert post(admin, "/admin/students/parent", student_id=str(users[key]["_id"]), parent_id=str(parent["_id"])).status_code == 303
    with app.app_context():
        provision(parent["name"], parent["email"], "parent", management=Auth0Management(), reuse=True)
        while deliver_next_onboarding():
            pass
    assert len(relay) == 1
    message = relay[0][0]
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "first.kid" in html and "second.kid" in html
    assert users["other_student"]["name"] not in html
    assert set(calls) == {parent["auth0_sub"], users["student"]["auth0_sub"], users["sibling"]["auth0_sub"]}
    assert database.onboarding_deliveries.count_documents({"state": "sent"}) == 1
    assert database.onboarding_deliveries.count_documents({"state": "skipped"}) == 2
    assert "https://unit-test.auth0.com/setup" not in str(list(database.onboarding_deliveries.find()))


def test_create_parent_with_children_sends_one_complete_welcome(app, database, users, relay, monkeypatch):
    configure_mail(app)
    setup_calls = setup_provider(monkeypatch)

    def create_subject(self, account, password):
        # The worker can run during any provider call without sending an early welcome.
        assert database.onboarding_deliveries.count_documents({}) == 0
        assert not deliver_next_onboarding()
        assert not database.users.find_one({"_id": account["_id"]})["active"]
        return "auth0|" + str(account["_id"])

    monkeypatch.setattr(Auth0Management, "create_subject", create_subject)
    admin = sign_in(app, users["admin"])
    assert post(admin, "/admin/accounts", name="Family Parent", email="family@example.test", role="parent",
                child_name=["First Child", "Second Child", ""], child_username=["First.Kid", "SECOND.KID", ""]).status_code == 303
    parent = database.users.find_one({"email": "family@example.test"})
    children = list(database.users.find({"parent_id": parent["_id"]}).sort("name", 1))
    assert parent["active"] and len(children) == 2 and all(child["active"] for child in children)
    assert [child["username"] for child in children] == ["first.kid", "second.kid"]
    assert all("email" not in child and "provisioning_parent_id" not in child for child in children)
    assert "provisioning_children" not in parent
    assert database.onboarding_deliveries.count_documents({}) == 1
    assert not setup_calls
    with app.app_context():
        assert deliver_next_onboarding()
        assert not deliver_next_onboarding()
    assert len(relay) == 1 and relay[0][0]["To"] == parent["email"]
    for format in ("plain", "html"):
        body = relay[0][0].get_body(preferencelist=(format,)).get_content()
        assert "First Child" in body and "first.kid" in body
        assert "Second Child" in body and "second.kid" in body
        assert all(str(account["_id"]) in body for account in [parent, *children])
    assert set(setup_calls) == {account["auth0_sub"] for account in [parent, *children]}
    assert "https://unit-test.auth0.com/setup" not in str(list(database.onboarding_deliveries.find()))


@pytest.mark.parametrize("children,status", [
    ({"child_name": ["Child"], "child_username": []}, 400),
    ({"child_name": ["Child"], "child_username": [""]}, 400),
    ({"child_name": [""], "child_username": ["valid.kid"]}, 400),
    ({"child_name": ["Child"], "child_username": ["bad username"]}, 400),
    ({"child_name": ["One", "Two"], "child_username": ["same.kid", "SAME.KID"]}, 400),
    ({"child_name": ["Child"] * 21, "child_username": ["many.kids"] * 21}, 400),
    ({"child_name": ["Child"], "child_username": ["taken.kid"]}, 409),
])
def test_family_validation_precedes_provisioning(app, database, users, monkeypatch, children, status):
    database.users.update_one({"_id": users["student"]["_id"]}, {"$set": {"username": "taken.kid"}})
    before = database.users.count_documents({})

    def unexpected_subject(*args, **kwargs):
        pytest.fail("Invalid family data must be rejected before creating an identity")

    monkeypatch.setattr(Auth0Management, "create_subject", unexpected_subject)
    admin = sign_in(app, users["admin"])
    assert post(admin, "/admin/accounts", name="Family Parent", email="family@example.test", role="parent", **children).status_code == status
    assert database.users.count_documents({}) == before
    assert database.onboarding_deliveries.count_documents({}) == 0


def test_partial_family_failure_can_retry_without_early_email_or_duplicate_accounts(app, database, users, relay, monkeypatch):
    configure_mail(app)
    setup_provider(monkeypatch)
    identities = {}

    def create_subject(self, account, password):
        if account.get("username") == "second.kid":
            raise ManagementError(503)
        return identities.setdefault(account["_id"], "auth0|" + str(account["_id"]))

    monkeypatch.setattr(Auth0Management, "create_subject", create_subject)
    admin = sign_in(app, users["admin"])
    details = {"name": "Family Parent", "email": "family@example.test", "role": "parent",
               "child_name": ["First Child", "Second Child"], "child_username": ["first.kid", "second.kid"]}
    response = post(admin, "/admin/accounts", **details)
    assert response.status_code == 502 and b"same parent and child details" in response.data
    pending_ids = set(database.users.distinct("_id", {"active": False}))
    assert len(pending_ids) == 3 and len(identities) == 2
    assert database.onboarding_deliveries.count_documents({}) == 0
    assert post(admin, "/admin/accounts", **{key: value for key, value in details.items() if not key.startswith("child_")}).status_code == 409
    assert post(admin, "/admin/accounts", name="First Child", username="first.kid", role="student").status_code == 409
    assert post(admin, "/admin/accounts", **{**details, "child_name": ["First Child"], "child_username": ["first.kid"]}).status_code == 409

    monkeypatch.setattr(Auth0Management, "create_subject", lambda self, account, password: identities.setdefault(account["_id"], "auth0|" + str(account["_id"])))
    assert post(admin, "/admin/accounts", **details).status_code == 303
    assert set(identities) == pending_ids
    assert database.users.count_documents({"_id": {"$in": list(pending_ids)}, "active": True}) == 3
    with app.app_context():
        assert deliver_next_onboarding()
        assert not deliver_next_onboarding()
    assert len(relay) == 1
    assert post(admin, "/admin/accounts", **details).status_code == 409
    assert database.onboarding_deliveries.count_documents({}) == 1


def test_only_parents_can_be_created_with_children(app, database, users):
    admin = sign_in(app, users["admin"])
    before = database.users.count_documents({})
    assert post(admin, "/admin/accounts", name="Tutor", email="new@example.test", role="tutor",
                child_name=["Child"], child_username=["new.child"]).status_code == 400
    assert database.users.count_documents({}) == before


@pytest.mark.parametrize("conflict", ["parent", "child"])
def test_auth0_identifier_conflicts_release_unused_family_reservations(app, database, users, monkeypatch, conflict):
    setup_provider(monkeypatch)
    identities = {}

    def create_subject(self, account, password):
        if (conflict == "parent" and account["role"] == "parent") or account.get("username") == "taken.kid":
            raise Problem("This identifier belongs to another Auth0 account.", 409)
        return identities.setdefault(account["_id"], "auth0|" + str(account["_id"]))

    monkeypatch.setattr(Auth0Management, "create_subject", create_subject)
    admin = sign_in(app, users["admin"])
    details = {"name": "Family Parent", "email": "family@example.test", "role": "parent",
               "child_name": ["First Child", "Second Child"], "child_username": ["first.kid", "taken.kid"]}
    assert post(admin, "/admin/accounts", **details).status_code == 409
    assert database.onboarding_deliveries.count_documents({}) == 0
    assert not database.users.find_one({"username": "taken.kid"})
    if conflict == "parent":
        assert not database.users.find_one({"email": details["email"]})
        assert not database.users.find_one({"username": "first.kid"})
        monkeypatch.setattr(Auth0Management, "existing_user", lambda *args: {"user_id": "auth0|existing-parent"})
        assert post(admin, "/admin/accounts/link", name=details["name"], email=details["email"], role="parent").status_code == 303
        assert database.onboarding_deliveries.find_one({})["existing_account"]
    else:
        parent = database.users.find_one({"email": details["email"]})
        assert not parent["active"] and len(identities) == 2
        assert post(admin, "/admin/accounts", **{**details, "child_username": ["first.kid", "second.kid"]}).status_code == 303
        assert database.users.count_documents({"parent_id": parent["_id"], "active": True}) == 2
        assert len(identities) == 3
        assert database.onboarding_deliveries.count_documents({}) == 1


def test_pending_child_can_be_deleted_and_replaced_during_family_retry(app, database, users, monkeypatch):
    def create_subject(self, account, password):
        if account["role"] == "student":
            raise ManagementError(503)
        return "auth0|" + str(account["_id"])

    monkeypatch.setattr(Auth0Management, "create_subject", create_subject)
    admin = sign_in(app, users["admin"])
    details = {"name": "Family Parent", "email": "family@example.test", "role": "parent",
               "child_name": ["Child"], "child_username": ["old.child"]}
    assert post(admin, "/admin/accounts", **details).status_code == 502
    pending = database.users.find_one({"username": "old.child"})
    assert post(admin, f"/admin/accounts/{pending['_id']}/delete").status_code == 303
    # An empty family reservation must still wait for an explicit family retry.
    assert post(admin, "/admin/accounts", name=details["name"], email=details["email"], role="parent").status_code == 409
    setup_provider(monkeypatch)
    assert post(admin, "/admin/accounts", **{**details, "child_username": ["new.child"]}).status_code == 303
    parent = database.users.find_one({"email": details["email"]})
    assert database.users.find_one({"username": "new.child"})["parent_id"] == parent["_id"]
    assert not database.users.find_one({"_id": pending["_id"]})["active"]
    assert database.onboarding_deliveries.count_documents({}) == 1


def test_deleted_child_during_family_setup_prevents_family_activation(app, database, users, monkeypatch):
    def create_subject(self, account, password):
        if account["role"] == "student":
            database.users.update_one({"_id": account["_id"]}, {"$set": {"deleted_at": now()}})
        return "auth0|" + str(account["_id"])

    monkeypatch.setattr(Auth0Management, "create_subject", create_subject)
    admin = sign_in(app, users["admin"])
    assert post(admin, "/admin/accounts", name="Family Parent", email="family@example.test", role="parent",
                child_name=["Child"], child_username=["new.child"]).status_code == 409
    assert not database.users.find_one({"email": "family@example.test"})["active"]
    assert not database.users.find_one({"username": "new.child"})["active"]
    assert database.onboarding_deliveries.count_documents({}) == 0


def test_parent_without_children_receives_followup_when_child_is_linked(app, database, users, relay, monkeypatch):
    configure_mail(app)
    calls = setup_provider(monkeypatch)
    with app.app_context():
        enqueue_onboarding(users["other_parent"]["_id"], existing_account=True)
        # Keep this parent unlinked until after their welcome email.
        database.users.update_one({"_id": users["other_student"]["_id"]}, {"$unset": {"parent_id": ""}})
        assert deliver_next_onboarding()
    assert calls == []
    assert "No children are linked yet" in relay[0][0].get_body(preferencelist=("plain",)).get_content()
    assert "Use your existing password" in relay[0][0].get_body(preferencelist=("plain",)).get_content()
    student = users["other_student"]
    database.users.update_one({"_id": student["_id"]}, {"$set": {"username": "new.child"}})
    admin = sign_in(app, users["admin"])
    for _ in range(2):
        assert post(admin, "/admin/students/parent", student_id=str(student["_id"]), parent_id=str(users["other_parent"]["_id"])).status_code == 303
    with app.app_context():
        assert deliver_next_onboarding()
        assert not deliver_next_onboarding()
    assert len(relay) == 2 and calls == [student["auth0_sub"]]
    assert relay[1][0]["To"] == users["other_parent"]["email"]
    assert "new.child" in relay[1][0].get_body(preferencelist=("plain",)).get_content()


def test_existing_auth0_parent_and_parent_role_change_queue_welcome(app, database, users, monkeypatch):
    setup_provider(monkeypatch)
    monkeypatch.setattr(Auth0Management, "existing_user", lambda self, email, subject=None: {"user_id": "auth0|existing-parent"})
    admin = sign_in(app, users["admin"])
    assert post(admin, "/admin/accounts/link", name="Existing Parent", email="existing@example.test", role="parent").status_code == 303
    account = database.users.find_one({"email": "existing@example.test"})
    assert database.onboarding_deliveries.find_one({"parent_id": account["_id"]})["existing_account"]
    candidate = {"_id": ObjectId(), "name": "New Role", "role": "tutor", "active": True,
                 "email": "role@example.test", "auth0_sub": "auth0|new-role"}
    database.users.insert_one(candidate)
    assert post(admin, f"/admin/accounts/{candidate['_id']}/role", role="parent").status_code == 303
    assert database.onboarding_deliveries.find_one({"parent_id": candidate["_id"]})["existing_account"]


def test_child_access_skips_changed_family_and_requeues_after_relink(app, database, users, relay, monkeypatch):
    configure_mail(app)
    calls = setup_provider(monkeypatch)
    admin = sign_in(app, users["admin"])
    student = users["student"]
    # A newly linked child is moved again before the worker sends their credentials.
    assert post(admin, "/admin/students/parent", student_id=str(student["_id"]), parent_id=str(users["other_parent"]["_id"])).status_code == 303
    assert post(admin, "/admin/students/parent", student_id=str(student["_id"]), parent_id="").status_code == 303
    with app.app_context():
        assert deliver_next_onboarding()
    assert not relay and not calls
    assert database.onboarding_deliveries.find_one({})["state"] == "skipped"
    assert post(admin, "/admin/students/parent", student_id=str(student["_id"]), parent_id=str(users["other_parent"]["_id"])).status_code == 303
    with app.app_context():
        assert deliver_next_onboarding()
    assert len(relay) == 1 and relay[0][0]["To"] == users["other_parent"]["email"]


def test_reassignment_during_auth0_setup_never_sends_old_parent_credentials(app, database, users, relay, monkeypatch):
    configure_mail(app)

    def setup_link(self, subject):
        database.users.update_one({"_id": users["student"]["_id"]}, {"$set": {"parent_id": users["other_parent"]["_id"]}})
        return "https://unit-test.auth0.com/private-child-ticket"

    monkeypatch.setattr(Auth0Management, "setup_link", setup_link)
    with app.app_context():
        enqueue_onboarding(users["parent"]["_id"], student_id=users["student"]["_id"])
        assert deliver_next_onboarding()
        assert not relay
        assert database.onboarding_deliveries.find_one({})["state"] == "queued"
        database.onboarding_deliveries.update_many({}, {"$set": {"next_at": now() - timedelta(minutes=1)}})
        assert deliver_next_onboarding()
    assert not relay
    assert database.onboarding_deliveries.find_one({})["state"] == "skipped"


@pytest.mark.parametrize("failure", ["auth0", "smtp"])
def test_onboarding_retries_configuration_and_delivery_failures_with_admin_recovery(app, database, users, relay, monkeypatch, failure):
    setup_provider(monkeypatch)
    with app.app_context():
        enqueue_onboarding(users["other_parent"]["_id"])
        app.config.update(SMTP_HOST="", MAIL_FROM="")
        assert not deliver_next_onboarding()
        assert database.onboarding_deliveries.find_one({})["attempts"] == 0
    configure_mail(app)

    def fail(*args):
        if failure == "auth0":
            raise ManagementError(503)
        raise smtplib.SMTPDataError(451, b"Temporary mail failure")

    if failure == "auth0":
        monkeypatch.setattr(Auth0Management, "setup_link", fail)
    else:
        monkeypatch.setattr("portal.onboarding.send_onboarding", fail)
    with app.app_context():
        for attempt in range(1, 4):
            database.onboarding_deliveries.update_many({}, {"$set": {"next_at": now() - timedelta(minutes=1)}})
            assert deliver_next_onboarding()
            assert database.onboarding_deliveries.find_one({})["attempts"] == attempt
        assert not deliver_next_onboarding()
    job = database.onboarding_deliveries.find_one({})
    assert job["state"] == "failed" and not relay
    retry = f"/admin/onboarding/{job['_id']}/retry"
    for role in ("parent", "student", "tutor"):
        assert post(sign_in(app, users[role]), retry).status_code == 403
    admin = sign_in(app, users["admin"])
    assert (b"Auth0 account management failed" if failure == "auth0" else b"Temporary mail failure") in admin.get("/admin/accounts").data
    assert post(admin, retry).status_code == 303
    setup_provider(monkeypatch)
    monkeypatch.setattr("portal.onboarding.send_onboarding", send_onboarding)
    with app.app_context():
        assert deliver_next_onboarding()
    assert len(relay) == 1
    assert database.onboarding_deliveries.find_one({})["state"] == "sent"
    assert post(admin, retry).status_code == 409


def test_expired_final_onboarding_lease_stops_without_sending(app, database, users, relay, monkeypatch):
    configure_mail(app)
    calls = setup_provider(monkeypatch)
    with app.app_context():
        enqueue_onboarding(users["parent"]["_id"])
        database.onboarding_deliveries.update_many({}, {"$set": {
            "state": "sending", "attempts": 3, "lease": "expired", "lease_until": now() - timedelta(minutes=1),
        }})
        assert not deliver_next_onboarding()
    assert database.onboarding_deliveries.find_one({})["state"] == "failed"
    assert not calls and not relay


def test_worker_once_delivers_queued_onboarding(app, database, users, relay, monkeypatch):
    configure_mail(app)
    setup_provider(monkeypatch)
    monkeypatch.setattr("portal.cli.maintain_series", lambda: None)
    monkeypatch.setattr("portal.cli.enqueue_reminders", lambda: None)
    with app.app_context():
        enqueue_onboarding(users["parent"]["_id"], existing_account=True)
    result = app.test_cli_runner().invoke(args=["invoice-worker", "--once"])
    assert result.exit_code == 0, result.output
    assert len(relay) == 1
