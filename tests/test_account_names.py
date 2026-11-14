from bson import ObjectId
from markupsafe import escape
import pytest

from portal import accounts
from portal.core import now
from test_portal import app, csrf, database, post, sign_in, users


@pytest.mark.parametrize("role, name", [
    ("admin", "Renamed Admin"),
    ("tutor", "A" * 100),
    ("parent", "  Renée 李 & O'Connor  "),
    ("student", '\"><script>alert(1)</script>'),
])
def test_admin_can_rename_active_accounts_including_self(app, database, users, role, name):
    account_id = users[role]["_id"]
    if role == "student":
        database.users.update_one({"_id": account_id}, {"$set": {"username": "student-one"}})
    original = database.users.find_one({"_id": account_id})
    signed_in = sign_in(app, users[role])
    admin = sign_in(app, users["admin"])
    path = f"/admin/accounts/{account_id}/name"
    assert f'action="{path}"'.encode() in admin.get("/admin/accounts").data

    response = post(admin, path, name=name)
    assert response.status_code == 303
    assert response.location == "/admin/accounts"
    saved = database.users.find_one({"_id": account_id})
    saved.pop("revision", None)
    assert saved == {**original, "name": name.strip()}

    page = admin.get(response.location)
    assert b"Account name saved." in page.data
    assert f'value="{escape(name.strip())}"'.encode() in page.data
    home = signed_in.get("/", follow_redirects=True)
    assert home.status_code == 200
    assert f"<strong>{escape(name.strip())}</strong>".encode() in home.data
    assert b"<script>alert(1)</script>" not in page.data


def test_renaming_requires_an_admin_and_csrf(app, database, users):
    account = users["student"]
    path = f"/admin/accounts/{account['_id']}/name"
    admin = sign_in(app, users["admin"])
    assert admin.get(path).status_code == 405
    assert admin.post(path, data={"name": "Changed"}).status_code == 400
    for role in ("tutor", "parent", "student"):
        assert post(sign_in(app, users[role]), path, name="Changed").status_code == 403

    anonymous = sign_in(app, users["admin"])
    token = csrf(anonymous)
    with anonymous.session_transaction() as session:
        session.pop("user_id")
    response = anonymous.post(path, data={"csrf_token": token, "name": "Changed"})
    assert response.status_code == 302
    assert response.location == "/login"
    assert post(admin, "/admin/accounts/invalid/name", name="Changed").status_code == 404
    assert database.users.find_one({"_id": account["_id"]}) == account


@pytest.mark.parametrize("fields", [{"role": "parent"}, {"active": False}], ids=["demoted", "disabled"])
def test_self_rename_rechecks_admin_access_before_saving(app, database, users, monkeypatch, fields):
    account = users["admin"]
    admin = sign_in(app, account)
    atomic = accounts.atomic

    def revoke_then_save(callback):
        database.users.update_one({"_id": account["_id"]}, {"$set": fields})
        return atomic(callback)

    monkeypatch.setattr(accounts, "atomic", revoke_then_save)
    assert post(admin, f"/admin/accounts/{account['_id']}/name", name="Changed").status_code == 409
    assert database.users.find_one({"_id": account["_id"]}) == {**account, **fields}


@pytest.mark.parametrize("name", ["", " \t\n ", "A" * 101, "Name\x00"])
def test_invalid_names_leave_the_account_unchanged(app, database, users, name):
    account = users["student"]
    admin = sign_in(app, users["admin"])
    path = f"/admin/accounts/{account['_id']}/name"
    assert post(admin, path, name=name).status_code == 400
    assert post(admin, path).status_code == 400
    assert database.users.find_one({"_id": account["_id"]}) == account


@pytest.mark.parametrize("state", ["missing", "provisioning", "deleting"])
def test_unavailable_accounts_cannot_be_renamed(app, database, users, state):
    account_id = ObjectId() if state == "missing" else users["student"]["_id"]
    if state != "missing":
        fields = {"active": False}
        if state == "deleting":
            fields.update(deletion_pending=True, deleted_at=now())
        database.users.update_one({"_id": account_id}, {"$set": fields})
    original = database.users.find_one({"_id": account_id})
    admin = sign_in(app, users["admin"])
    path = f"/admin/accounts/{account_id}/name"
    assert f'action="{path}"'.encode() not in admin.get("/admin/accounts").data
    assert post(admin, path, name="Changed").status_code == 404
    assert database.users.find_one({"_id": account_id}) == original
