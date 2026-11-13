from datetime import timedelta
import json
from urllib.parse import unquote, urlsplit

from bson import ObjectId
import pytest
import requests

from portal.accounts import Auth0Management
from portal.billing import generate
from portal.core import now
from portal.onboarding import enqueue_onboarding
from test_portal import app, database, lesson_record, post, sign_in, users


@pytest.fixture(autouse=True)
def provider(app, users, monkeypatch):
    identities = {
        account["auth0_sub"]: {
            "user_id": account["auth0_sub"], "email": account["email"],
            "identities": [{"connection": app.config["AUTH0_CONNECTION"]}],
            "app_metadata": {"lesson_desk_account": str(account["_id"])},
        } for account in users.values()
    }
    state = {"identities": identities, "calls": [], "delete_status": 204, "fail_username": None}

    def api(method, url, **kwargs):
        path = urlsplit(url).path
        state["calls"].append((method, path))
        status, payload = 200, {}
        if path == "/oauth/token":
            payload = {"access_token": "test-management-token"}
        else:
            assert kwargs["headers"]["Authorization"] == "Bearer test-management-token"
            if path == "/api/v2/users-by-email":
                payload = [identity for identity in identities.values() if identity.get("email") == kwargs["params"]["email"]]
            elif path == "/api/v2/users" and method == "GET":
                username = json.loads(kwargs["params"]["q"].split(" AND identities.connection:")[0].removeprefix("username:"))
                payload = [identity for identity in identities.values() if identity.get("username") == username]
            elif path == "/api/v2/users" and method == "POST":
                body = kwargs["json"]
                if state["fail_username"] and body.get("username") == state["fail_username"]:
                    status = 503
                else:
                    assert not any(
                        (body.get("email") and identity.get("email") == body["email"])
                        or (body.get("username") and identity.get("username") == body["username"])
                        for identity in identities.values()
                    ), "Auth0 identifiers must be released before account creation"
                    subject = "auth0|" + str(ObjectId())
                    payload = {**body, "user_id": subject, "identities": [{"connection": body["connection"]}]}
                    identities[subject] = payload
            elif method == "DELETE":
                assert path.startswith("/api/v2/users/")
                status = state["delete_status"]
                if status == 204:
                    identities.pop(unquote(path.removeprefix("/api/v2/users/")), None)
            elif path == "/api/v2/tickets/password-change":
                payload = {"ticket": "https://unit-test.auth0.com/setup"}
            else:
                pytest.fail(f"Unexpected Auth0 request: {method} {path}")
        response = requests.Response()
        response.status_code = status
        response._content = b"" if status == 204 else json.dumps(payload).encode()
        return response

    monkeypatch.setattr(requests, "request", api)
    return state


def test_account_deletion_requires_admin_confirmation_and_csrf(app, database, users):
    target = users["student"]
    path = f"/admin/accounts/{target['_id']}/delete"
    admin = sign_in(app, users["admin"])
    assert path.encode() in admin.get("/admin/accounts").data
    assert f"/admin/accounts/{users['admin']['_id']}/delete".encode() not in admin.get("/admin/accounts").data
    page = admin.get(path)
    assert page.status_code == 200 and b"Keep account" in page.data
    assert database.users.find_one({"_id": target["_id"]})["active"]
    assert admin.post(path).status_code == 400
    for role in ("parent", "tutor", "student"):
        client = sign_in(app, users[role])
        assert client.get(path).status_code == 403
        assert post(client, path).status_code == 403
    own_path = f"/admin/accounts/{users['admin']['_id']}/delete"
    assert admin.get(own_path).status_code == 409
    assert post(admin, own_path).status_code == 409
    assert post(admin, f"/admin/accounts/{ObjectId()}/delete").status_code == 404
    assert admin.get("/admin/accounts/invalid/delete").status_code == 404
    assert database.users.find_one({"_id": target["_id"]})["active"]


@pytest.mark.parametrize("role", ["student", "tutor", "parent"])
def test_deleted_accounts_lose_access_and_links_but_keep_history(app, database, users, provider, role):
    account = users[role]
    account_id = account["_id"]
    family_id = users["parent"]["_id"]
    student_id = users["student"]["_id"]
    student = sign_in(app, users["student"])
    signed_in = sign_in(app, account)
    admin = sign_in(app, users["admin"])
    lesson = lesson_record(database, users, "student", now() + timedelta(days=2), billing_parent_id=family_id)
    invoice_id = database.invoices.insert_one({"number": "HISTORY", "parent_id": family_id, "tutor_id": users["tutor"]["_id"]}).inserted_id
    homework_id = database.homework.insert_one({"student_id": student_id, "tutor_id": users["tutor"]["_id"]}).inserted_id
    series_id = database.lesson_series.insert_one({"active": True, "student_id": student_id, "tutor_id": users["tutor"]["_id"]}).inserted_id
    change_id = database.change_requests.insert_one({
        "status": "pending", "parent_id": family_id, "student_id": student_id,
        "tutor_id": users["tutor"]["_id"], "lesson_id": lesson["_id"],
    }).inserted_id
    with app.app_context():
        enqueue_onboarding(family_id, student_id=student_id if role == "student" else None)
    database.lesson_reminder_deliveries.insert_one({"_id": "pending", "user_id": account_id, "state": "sending", "lease": "old"})
    database.users.update_one({"_id": account_id}, {"$set": {"calendar_version": "private-calendar"}})

    path = f"/admin/accounts/{account_id}/delete"
    assert post(admin, path).status_code == 303
    assert database.users.find_one({"_id": account_id}) is None
    assert account["auth0_sub"] not in provider["identities"]
    deleted = database.account_history.find_one({"_id": account_id})
    assert deleted["deleted_at"] and deleted["deleted_by"] == users["admin"]["_id"]
    assert set(deleted) == {"_id", "name", "role", "deleted_at", "deleted_by"}
    assert not database.enrollments.find_one({"$or": [{"tutor_id": account_id}, {"student_id": account_id}]})
    assert not database.lesson_series.find_one({"_id": series_id})["active"]
    assert database.change_requests.find_one({"_id": change_id})["status"] == "withdrawn"
    assert database.lessons.find_one({"_id": lesson["_id"]})["billing_parent_id"] == family_id
    assert database.invoices.find_one({"_id": invoice_id})
    assert database.homework.find_one({"_id": homework_id})
    assert database.lesson_reminder_deliveries.find_one({"_id": "pending"})["state"] == "skipped"
    assert database.users.count_documents({"parent_id": account_id}) == 0
    assert signed_in.get("/profile").status_code == 302
    with signed_in.session_transaction() as session:
        assert "user_id" not in session
    assert str(account_id).encode() not in admin.get("/admin/accounts").data
    assert admin.get("/admin/students").status_code == 200
    assert post(admin, path).status_code == 404
    assert post(admin, f"/admin/accounts/{account_id}/setup").status_code == 404
    if role == "parent":
        assert student.get("/homework").status_code == 200
        assert database.users.find_one({"_id": student_id})["active"]
        assert database.enrollments.find_one({"student_id": student_id})
        assert post(admin, "/admin/students/parent", student_id=str(student_id), parent_id=str(users["other_parent"]["_id"])).status_code == 303
    if role in ("parent", "student"):
        assert database.onboarding_deliveries.find_one({})["state"] == "skipped"


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("method", ["create", "family", "link"])
def test_deleted_email_can_be_used_by_a_fresh_account(app, database, users, monkeypatch, provider, pending, method):
    account = users["other_parent"]
    if pending:
        database.users.update_one({"_id": account["_id"]}, {"$set": {"active": False}, "$unset": {"auth0_sub": ""}})
    admin = sign_in(app, users["admin"])
    assert post(admin, f"/admin/accounts/{account['_id']}/delete").status_code == 303
    monkeypatch.setattr(app.extensions["auth0"], "authorize_access_token", lambda **kwargs: {
        "userinfo": {"aud": app.config["AUTH0_CLIENT_ID"], "sub": account["auth0_sub"]},
    })
    assert app.test_client().get("/auth/callback").status_code == 403
    data = {"name": "New Parent Name", "email": account["email"].upper(), "role": "parent"}
    if method == "family":
        data.update(child_name=["New Child"], child_username=["new.child"])
    if method == "link":
        provider["identities"]["auth0|replacement"] = {
            "user_id": "auth0|replacement", "email": account["email"],
            "identities": [{"connection": app.config["AUTH0_CONNECTION"]}],
        }
    assert post(admin, "/admin/accounts/link" if method == "link" else "/admin/accounts", **data).status_code == 303
    replacement = database.users.find_one({"email": account["email"]})
    assert replacement["active"] and replacement["_id"] != account["_id"]
    assert replacement["auth0_sub"] != account["auth0_sub"]
    assert database.users.find_one({"_id": account["_id"]}) is None
    assert database.users.count_documents({"parent_id": replacement["_id"]}) == (1 if method == "family" else 0)
    assert database.onboarding_deliveries.count_documents({"parent_id": replacement["_id"]}) == 1


def test_deleting_account_during_provisioning_prevents_activation(app, database, users, monkeypatch):
    admin = sign_in(app, users["admin"])

    def create_subject(self, account, password):
        database.users.update_one({"_id": account["_id"]}, {"$set": {"active": False, "deleted_at": now()}})
        return "auth0|deleted-during-setup"

    monkeypatch.setattr(Auth0Management, "create_subject", create_subject)
    assert post(admin, "/admin/accounts", name="Pending", email="pending@example.test", role="parent").status_code == 409
    assert not database.users.find_one({"email": "pending@example.test"})["active"]
    assert database.onboarding_deliveries.count_documents({}) == 0


@pytest.mark.parametrize("family", [False, True])
def test_accounts_deleted_by_older_versions_release_identifiers_on_creation(app, database, users, provider, family):
    account = users["other_parent"]
    database.users.update_one({"_id": account["_id"]}, {"$set": {"active": False, "deleted_at": now()}})
    admin = sign_in(app, users["admin"])
    data = {"name": "Replacement Parent", "email": account["email"], "role": "parent"}
    if family:
        data.update(child_name=["Child"], child_username=["reused.child"])
        child_id = database.users.insert_one({
            "name": "Old Child", "username": "reused.child", "role": "student", "active": False,
            "deleted_at": now(), "auth0_sub": "auth0|old-child",
        }).inserted_id
        provider["identities"]["auth0|old-child"] = {
            "user_id": "auth0|old-child", "username": "reused.child",
            "identities": [{"connection": app.config["AUTH0_CONNECTION"]}],
        }
    assert post(admin, "/admin/accounts", **data).status_code == 303
    replacement = database.users.find_one({"email": account["email"]})
    assert replacement["active"] and replacement["_id"] != account["_id"]
    assert database.users.find_one({"_id": account["_id"]}) is None
    assert account["auth0_sub"] not in provider["identities"]
    assert database.account_history.find_one({"_id": account["_id"]})
    if family:
        assert database.users.find_one({"_id": child_id}) is None
        assert "auth0|old-child" not in provider["identities"]
        assert database.users.find_one({"username": "reused.child"})["parent_id"] == replacement["_id"]


@pytest.mark.parametrize("email", [False, True])
def test_deleted_student_username_and_optional_email_can_be_reused(app, database, users, provider, email):
    account = users["student"]
    fields = {"$set": {"username": "reused.student"}}
    if not email:
        fields["$unset"] = {"email": ""}
    database.users.update_one({"_id": account["_id"]}, fields)
    provider["identities"][account["auth0_sub"]]["username"] = "reused.student"
    admin = sign_in(app, users["admin"])
    signed_in = sign_in(app, account)
    assert post(admin, f"/admin/accounts/{account['_id']}/delete").status_code == 303
    data = {"name": "Replacement Student", "username": "REUSED.STUDENT", "role": "student"}
    if email:
        data["email"] = account["email"]
    assert post(admin, "/admin/accounts", **data).status_code == 201
    replacement = database.users.find_one({"username": "reused.student"})
    assert replacement["active"] and replacement["_id"] != account["_id"]
    assert "parent_id" not in replacement
    assert signed_in.get("/homework").status_code == 302


@pytest.mark.parametrize("status", [403, 429, 503])
def test_failed_auth0_deletion_revokes_access_and_can_be_retried(app, database, users, provider, status):
    account = users["other_parent"]
    admin = sign_in(app, users["admin"])
    signed_in = sign_in(app, account)
    path = f"/admin/accounts/{account['_id']}/delete"
    provider["delete_status"] = status
    response = post(admin, path)
    assert response.status_code == 502 and b"delete:users" in response.data
    pending = database.users.find_one({"_id": account["_id"]})
    assert pending["deletion_pending"] and not pending["active"]
    assert pending["email"] == account["email"] and pending["auth0_sub"] == account["auth0_sub"]
    assert signed_in.get("/lessons").status_code == 302
    page = admin.get("/admin/accounts")
    assert b"Deletion pending" in page.data and path.encode() in page.data
    assert post(admin, f"/admin/accounts/{account['_id']}/setup").status_code == 404
    assert post(admin, "/admin/accounts", name="Replacement", email=account["email"], role="parent").status_code == 502
    assert database.users.count_documents({"email": account["email"]}) == 1
    provider["delete_status"] = 204
    assert post(admin, path).status_code == 303
    assert database.users.find_one({"_id": account["_id"]}) is None
    assert post(admin, "/admin/accounts", name="Replacement", email=account["email"], role="parent").status_code == 303


def test_deletion_succeeds_if_auth0_identity_is_already_absent(app, database, users, provider):
    account = users["other_parent"]
    provider["identities"].pop(account["auth0_sub"])
    admin = sign_in(app, users["admin"])
    assert post(admin, f"/admin/accounts/{account['_id']}/delete").status_code == 303
    assert database.users.find_one({"_id": account["_id"]}) is None
    assert post(admin, "/admin/accounts", name="Replacement", email=account["email"], role="parent").status_code == 303


def test_deleting_pending_family_removes_partial_identities_and_child_reservations(app, database, users, provider):
    provider["fail_username"] = "second.child"
    admin = sign_in(app, users["admin"])
    details = {"name": "Family Parent", "email": "family@example.test", "role": "parent",
               "child_name": ["First Child", "Second Child"], "child_username": ["first.child", "second.child"]}
    assert post(admin, "/admin/accounts", **details).status_code == 502
    parent = database.users.find_one({"email": details["email"]})
    ids = set(database.users.distinct("_id", {"active": False}))
    assert len(ids) == 3
    assert post(admin, f"/admin/accounts/{parent['_id']}/delete").status_code == 303
    assert database.users.count_documents({"_id": {"$in": list(ids)}}) == 0
    assert not any(identity.get("app_metadata", {}).get("lesson_desk_account") in {str(key) for key in ids}
                   for identity in provider["identities"].values())
    provider["fail_username"] = None
    assert post(admin, "/admin/accounts", **details).status_code == 303
    replacement = database.users.find_one({"email": details["email"]})
    assert replacement["active"] and replacement["_id"] not in ids
    assert database.users.count_documents({"parent_id": replacement["_id"], "active": True}) == 2
    assert database.onboarding_deliveries.count_documents({}) == 1


def test_pending_account_deletion_preserves_an_unrelated_auth0_identity(app, database, users, provider):
    account = users["other_parent"]
    database.users.update_one({"_id": account["_id"]}, {"$set": {"active": False}, "$unset": {"auth0_sub": ""}})
    provider["identities"][account["auth0_sub"]]["app_metadata"] = {"lesson_desk_account": "a-different-account"}
    admin = sign_in(app, users["admin"])
    assert post(admin, f"/admin/accounts/{account['_id']}/delete").status_code == 303
    assert database.users.find_one({"_id": account["_id"]}) is None
    assert account["auth0_sub"] in provider["identities"]
    assert not any(method == "DELETE" for method, _ in provider["calls"])


def test_deleted_student_history_remains_readable_and_billable(app, database, users):
    account = users["student"]
    lesson = lesson_record(database, users, "student", now() - timedelta(days=2), status="completed",
                           charge=True, billing_parent_id=users["parent"]["_id"])
    homework_id = database.homework.insert_one({
        "student_id": account["_id"], "tutor_id": users["tutor"]["_id"],
        "title": "Historical homework", "created_at": now(),
    }).inserted_id
    admin = sign_in(app, users["admin"])
    assert post(admin, f"/admin/accounts/{account['_id']}/delete").status_code == 303
    tutor = sign_in(app, users["tutor"])
    for path in ("/lessons", f"/lessons/{lesson['_id']}", f"/lessons/{lesson['_id']}/whiteboard", "/homework", f"/homework/{homework_id}"):
        response = tutor.get(path)
        assert response.status_code == 200, path
        assert account["name"].encode() in response.data, path
    with app.app_context():
        invoice_id = generate(users["tutor"]["_id"], users["parent"]["_id"])
    invoice = database.invoices.find_one({"_id": invoice_id})
    assert invoice["lines"][0]["student"] == account["name"]


@pytest.mark.parametrize("family", [False, True])
def test_identity_created_after_deletion_is_cleaned_up_even_if_family_setup_fails(app, database, users, provider, monkeypatch, family):
    admin = sign_in(app, users["admin"])
    original_create = Auth0Management.create_subject
    account_ids = []

    def create_subject(self, account, password):
        if account["role"] == "parent":
            account_ids.extend(database.users.distinct("_id", {"active": False}))
            assert post(admin, f"/admin/accounts/{account['_id']}/delete").status_code == 303
        return original_create(self, account, password)

    monkeypatch.setattr(Auth0Management, "create_subject", create_subject)
    data = {"name": "Pending Parent", "email": "pending@example.test", "role": "parent"}
    if family:
        data.update(child_name=["Pending Child"], child_username=["pending.child"])
        provider["fail_username"] = "pending.child"
    assert post(admin, "/admin/accounts", **data).status_code == (502 if family else 409)
    assert database.users.count_documents({"_id": {"$in": account_ids}}) == 0
    assert not any(identity.get("app_metadata", {}).get("lesson_desk_account") in {str(key) for key in account_ids}
                   for identity in provider["identities"].values())
    assert database.onboarding_deliveries.count_documents({}) == 0
