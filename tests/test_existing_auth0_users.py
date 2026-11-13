import pytest

from portal.accounts import Auth0Management
from test_portal import app, database, users, post, sign_in


def auth0_identity(subject="auth0|existing", **changes):
    return {
        "user_id": subject, "email": "existing@example.test", "name": "Existing User",
        "identities": [{"connection": "Username-Password-Authentication", "provider": "auth0"}],
        "app_metadata": {"another_app_role": "owner"}, **changes,
    }


def mock_lookup(monkeypatch, identities):
    calls = []

    def call(self, method, path, **kwargs):
        calls.append((method, path))
        if path == "/oauth/token":
            return {"access_token": "test-management-token"}
        assert method == "GET" and path == "/api/v2/users-by-email"
        assert kwargs["params"] == {"email": "existing@example.test"}
        return identities

    monkeypatch.setattr(Auth0Management, "call", call)
    return calls


def test_admin_links_existing_identity_and_preserves_provider_account(app, database, users, monkeypatch):
    identity = auth0_identity()
    calls = mock_lookup(monkeypatch, [identity])
    admin = sign_in(app, users["admin"])
    assert post(admin, "/admin/accounts/link", name="Existing User", email="EXISTING@example.test", role="parent").status_code == 303
    account = database.users.find_one({"auth0_sub": identity["user_id"]})
    assert account["email"] == "existing@example.test" and account["active"] and account["role"] == "parent"
    assert sign_in(app, account).get("/lessons").status_code == 200
    assert identity["app_metadata"] == {"another_app_role": "owner"}
    assert calls == [("POST", "/oauth/token"), ("GET", "/api/v2/users-by-email")]
    assert post(admin, "/admin/accounts/link", name="Change role", email=account["email"], role="admin").status_code == 409
    assert database.users.find_one({"_id": account["_id"]})["role"] == "parent"


def test_link_existing_identity_finishes_pending_creation(app, database, users, monkeypatch):
    mock_lookup(monkeypatch, [auth0_identity()])
    account_id = database.users.insert_one({
        "name": "Existing User", "email": "existing@example.test", "role": "tutor", "active": False,
    }).inserted_id
    admin = sign_in(app, users["admin"])
    assert post(admin, "/admin/accounts/link", name="Existing User", email="existing@example.test", role="tutor").status_code == 303
    account = database.users.find_one({"_id": account_id})
    assert account["auth0_sub"] == "auth0|existing" and account["active"]


@pytest.mark.parametrize("identities,status", [
    ([], 404),
    ([auth0_identity(identities=[{"connection": "Other-Database"}])], 404),
    ([auth0_identity(blocked=True)], 409),
    ([auth0_identity(), auth0_identity("auth0|ambiguous")], 409),
])
def test_link_rejects_missing_ambiguous_blocked_or_wrong_connection_users(app, database, users, monkeypatch, identities, status):
    mock_lookup(monkeypatch, identities)
    before = database.users.count_documents({})
    assert post(sign_in(app, users["admin"]), "/admin/accounts/link", name="Existing User", email="existing@example.test", role="tutor").status_code == status
    assert database.users.count_documents({}) == before


def test_only_admins_link_and_explicit_subject_selects_one_identity(app, database, users, monkeypatch):
    calls = mock_lookup(monkeypatch, [auth0_identity(), auth0_identity("auth0|selected")])
    for role in ("parent", "tutor", "student"):
        assert post(sign_in(app, users[role]), "/admin/accounts/link", name="Existing User", email="existing@example.test", role="admin").status_code == 403
    assert not calls
    admin = sign_in(app, users["admin"])
    assert post(admin, "/admin/accounts/link", name="Existing User", email="existing@example.test", role="tutor", auth0_sub="auth0|selected").status_code == 303
    assert database.users.find_one({"email": "existing@example.test"})["auth0_sub"] == "auth0|selected"


def test_existing_identity_cli_can_add_first_admin(app, database, monkeypatch):
    mock_lookup(monkeypatch, [auth0_identity()])
    result = app.test_cli_runner().invoke(args=[
        "link-auth0-user", "--name", "Existing User", "--email", "existing@example.test", "--role", "admin",
    ])
    assert result.exit_code == 0, result.output
    assert database.users.find_one({"email": "existing@example.test"})["role"] == "admin"
