import json
from urllib.parse import urlsplit

from bson import ObjectId
from pymongo.errors import DuplicateKeyError
import pytest
import requests

from portal.accounts import Auth0Management, ManagementError, username_field
from portal.core import Problem, ensure_indexes
from test_portal import app, database, post, sign_in, users


def test_username_only_student_accounts_are_unique_and_optional_email_works(app, database, users, monkeypatch):
    created = []

    def create_subject(self, account, password):
        created.append(account)
        return "auth0|" + str(account["_id"])

    monkeypatch.setattr(Auth0Management, "create_subject", create_subject)
    monkeypatch.setattr(Auth0Management, "setup_link", lambda self, subject: "https://unit-test.auth0.com/setup")
    admin = sign_in(app, users["admin"])
    for name, username in (("First student", "First.Kid"), ("Second student", "second-kid")):
        response = post(admin, "/admin/accounts", name=name, role="student", username=username)
        assert response.status_code == 201
        assert username.lower().encode() in response.data
        assert b"No email is needed" in response.data
    assert all("email" not in account for account in created)
    assert database.users.count_documents({"email": {"$exists": False}}) == 2
    assert post(admin, "/admin/accounts", name="Duplicate", role="student", username="FIRST.KID").status_code == 409
    assert post(admin, "/admin/accounts", name="Missing username", role="student", email="kid@example.test").status_code == 400
    assert post(admin, "/admin/accounts", name="Adult", role="parent", username="adult").status_code == 400
    assert sign_in(app, users["parent"]).post("/admin/accounts", data={}).status_code in {400, 403}


def test_existing_student_username_sync_retries_without_changing_subject(app, database, users, monkeypatch):
    calls = []

    def set_username(self, subject, username):
        calls.append((subject, username))
        if len(calls) == 1:
            raise ManagementError(503)

    monkeypatch.setattr(Auth0Management, "set_username", set_username)
    admin = sign_in(app, users["admin"])
    path = f"/admin/accounts/{users['student']['_id']}/username"
    assert post(admin, path, username="Student.New").status_code == 502
    pending = database.users.find_one({"_id": users["student"]["_id"]})
    assert pending["username"] == "student.new" and pending["username_sync_pending"]
    assert post(admin, f"/admin/accounts/{users['student']['_id']}/setup").status_code == 409
    assert post(admin, path, username="another-name").status_code == 409
    assert post(admin, path, username="Student.New").status_code == 303
    student = database.users.find_one({"_id": users["student"]["_id"]})
    assert student["auth0_sub"] == users["student"]["auth0_sub"]
    assert student["parent_id"] == users["parent"]["_id"]
    assert not student.get("username_sync_pending")
    assert calls == [(student["auth0_sub"], "student.new")] * 2
    assert post(admin, f"/admin/accounts/{users['sibling']['_id']}/username", username="STUDENT.NEW").status_code == 409


def test_username_only_account_cannot_become_an_adult_without_email(app, database, users):
    account = {"_id": ObjectId(), "role": "student", "username": "no-email", "name": "Student",
               "active": True, "auth0_sub": "auth0|no-email"}
    database.users.insert_one(account)
    admin = sign_in(app, users["admin"])
    assert post(admin, f"/admin/accounts/{account['_id']}/role", role="parent").status_code == 409
    assert database.users.find_one({"_id": account["_id"]})["role"] == "student"


def test_pending_username_blocks_role_changes_until_sync_finishes(app, database, users, monkeypatch):
    student_id = users["other_student"]["_id"]
    database.users.update_one({"_id": student_id}, {"$unset": {"parent_id": ""}})
    database.enrollments.delete_many({"student_id": student_id})
    database.users.update_one({"_id": student_id}, {"$set": {
        "username": "pending-kid", "username_sync_pending": True,
    }})
    admin = sign_in(app, users["admin"])
    role_path = f"/admin/accounts/{student_id}/role"
    assert post(admin, role_path, role="tutor").status_code == 409
    assert database.users.find_one({"_id": student_id})["role"] == "student"
    monkeypatch.setattr(Auth0Management, "set_username", lambda self, subject, username: None)
    assert post(admin, f"/admin/accounts/{student_id}/username", username="pending-kid").status_code == 303
    assert post(admin, role_path, role="tutor").status_code == 303


def test_synced_student_username_cannot_be_renamed(app, database, users, monkeypatch):
    student_id = users["student"]["_id"]
    database.users.update_one({"_id": student_id}, {"$set": {"username": "original-kid"}})
    calls = []
    monkeypatch.setattr(Auth0Management, "set_username", lambda self, subject, username: calls.append(username))
    admin = sign_in(app, users["admin"])
    path = f"/admin/accounts/{student_id}/username"
    assert post(admin, path, username="different-kid").status_code == 409
    assert post(admin, path, username="original-kid").status_code == 303
    assert calls == []
    assert database.users.find_one({"_id": student_id})["username"] == "original-kid"
    assert f'action="{path}"'.encode() not in admin.get("/admin/accounts").data


def test_auth0_username_conflict_releases_reservation(app, database, users, monkeypatch):
    def conflict(self, subject, username):
        raise ManagementError(409)

    monkeypatch.setattr(Auth0Management, "set_username", conflict)
    admin = sign_in(app, users["admin"])
    path = f"/admin/accounts/{users['student']['_id']}/username"
    assert post(admin, path, username="taken-name").status_code == 409
    student = database.users.find_one({"_id": users["student"]["_id"]})
    assert "username" not in student and not student.get("username_sync_pending")
    monkeypatch.setattr(Auth0Management, "set_username", lambda self, subject, username: None)
    assert post(admin, path, username="another-name").status_code == 303


def test_auth0_username_provisioning_omits_email_and_requires_portal_marker(app, monkeypatch):
    account = {"_id": ObjectId(), "name": "Student", "username": "student-one"}
    provider_users = []
    bodies = []

    def api(method, url, **kwargs):
        path = urlsplit(url).path
        if path == "/oauth/token":
            payload = {"access_token": "management-test"}
        elif method == "GET":
            assert path == "/api/v2/users"
            assert kwargs["params"]["q"] == 'username:"student-one" AND identities.connection:' + json.dumps(app.config["AUTH0_CONNECTION"])
            assert "connection" not in kwargs["params"]
            payload = provider_users
        else:
            assert method == "POST" and path == "/api/v2/users"
            body = kwargs["json"]
            assert "email" not in body and "verify_email" not in body
            assert body["username"] == account["username"]
            bodies.append(body)
            payload = {"user_id": "auth0|new-student"}
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(requests, "request", api)
    with app.app_context():
        management = Auth0Management()
        assert management.create_subject(account, "StrongPasswordOnlyForTest7!") == "auth0|new-student"
        provider_users.append({"user_id": "auth0|foreign", "username": "student-one",
                               "identities": [{"connection": management.connection}], "app_metadata": {}})
        with pytest.raises(Problem, match="outside this portal"):
            management.create_subject(account, "StrongPasswordOnlyForTest7!")
        provider_users[0]["app_metadata"]["lesson_desk_account"] = str(account["_id"])
        assert management.create_subject(account, "StrongPasswordOnlyForTest7!") == "auth0|foreign"
    assert len(bodies) == 1


def test_legacy_email_index_migrates_without_losing_uniqueness(app, database):
    database.users.drop_indexes()
    database.users.create_index("email", unique=True)
    database.users.insert_one({"email": "existing@example.test"})
    with app.app_context():
        ensure_indexes()
    assert "email_1" not in database.users.index_information()
    database.users.insert_many([
        {"username": "first-student"}, {"username": "second-student"},
    ])
    with pytest.raises(DuplicateKeyError):
        database.users.insert_one({"email": "existing@example.test"})
    with pytest.raises(DuplicateKeyError):
        database.users.insert_one({"username": "first-student"})


@pytest.mark.parametrize("value", ["a", "ab", "white space", "kid@example.test", "x" * 16, "-kid", "用户名"])
def test_invalid_username_is_rejected(value):
    with pytest.raises(Problem):
        username_field({"username": value})
