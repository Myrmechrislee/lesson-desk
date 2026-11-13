from datetime import timedelta

from bson import ObjectId
import pytest

from portal.accounts import Auth0Management
from portal.core import now
from portal.onboarding import enqueue_onboarding
from test_portal import app, database, lesson_record, post, sign_in, users


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
def test_deleted_accounts_lose_access_and_links_but_keep_history(app, database, users, monkeypatch, role):
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

    def unexpected_provider_call(*args, **kwargs):
        pytest.fail("Deleting portal access must preserve the shared Auth0 identity")

    monkeypatch.setattr(Auth0Management, "call", unexpected_provider_call)
    path = f"/admin/accounts/{account_id}/delete"
    assert post(admin, path).status_code == 303
    deleted = database.users.find_one({"_id": account_id})
    assert not deleted["active"] and deleted["deleted_at"] and deleted["deleted_by"] == users["admin"]["_id"]
    assert deleted["auth0_sub"] == account["auth0_sub"] and "calendar_version" not in deleted
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
def test_deleted_accounts_cannot_be_reactivated_by_creation_linking_or_login(app, database, users, monkeypatch, pending):
    account = users["other_parent"]
    if pending:
        database.users.update_one({"_id": account["_id"]}, {"$set": {"active": False}, "$unset": {"auth0_sub": ""}})
    admin = sign_in(app, users["admin"])
    assert post(admin, f"/admin/accounts/{account['_id']}/delete").status_code == 303
    assert post(admin, "/admin/accounts", name=account["name"], email=account["email"], role="parent").status_code == 409
    monkeypatch.setattr(Auth0Management, "existing_user", lambda *args: {"user_id": account["auth0_sub"]})
    assert post(admin, "/admin/accounts/link", name=account["name"], email=account["email"], role="parent").status_code == 409
    monkeypatch.setattr(app.extensions["auth0"], "authorize_access_token", lambda **kwargs: {
        "userinfo": {"aud": app.config["AUTH0_CLIENT_ID"], "sub": account["auth0_sub"]},
    })
    assert app.test_client().get("/auth/callback").status_code == 403
    assert not database.users.find_one({"_id": account["_id"]})["active"]
    assert database.onboarding_deliveries.count_documents({}) == 0


def test_deleting_account_during_provisioning_prevents_activation(app, database, users, monkeypatch):
    admin = sign_in(app, users["admin"])

    def create_subject(self, account, password):
        database.users.update_one({"_id": account["_id"]}, {"$set": {"active": False, "deleted_at": now()}})
        return "auth0|deleted-during-setup"

    monkeypatch.setattr(Auth0Management, "create_subject", create_subject)
    assert post(admin, "/admin/accounts", name="Pending", email="pending@example.test", role="parent").status_code == 409
    assert not database.users.find_one({"email": "pending@example.test"})["active"]
    assert database.onboarding_deliveries.count_documents({}) == 0
