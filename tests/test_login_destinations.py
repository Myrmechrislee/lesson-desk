from types import SimpleNamespace

from flask import Flask, g, redirect
import pytest

from portal.accounts import bp


@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("destination, expected", [
    (None, "/"),
    ("/invoices/123/pdf", "/invoices/123/pdf"),
    ("/lessons?student=123", "/lessons?student=123"),
    ("https://outside.example/path", "/"),
    ("//outside.example/path", "/"),
    ("/\\outside.example/path", "/"),
    ("/%2foutside.example/path", "/"),
    ("/%5coutside.example/path", "/"),
    ("/invoices\r\nLocation: https://outside.example", "/"),
    ("javascript:alert(1)", "/"),
])
def test_login_only_returns_to_local_paths(authenticated, destination, expected):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="local-login-test", APP_BASE_URL="https://lessons.example.test",
        AUTH0_CONNECTION="test-database",
    )
    app.register_blueprint(bp)
    app.add_url_rule("/", endpoint="home", view_func=lambda: "Home")
    app.extensions["auth0"] = SimpleNamespace(authorize_redirect=lambda *args, **kwargs: redirect("/oidc"))

    @app.before_request
    def account():
        g.user = {"role": "parent"} if authenticated else None

    client = app.test_client()
    response = client.get("/login", query_string={"next": destination} if destination is not None else {})
    if authenticated:
        assert response.location == expected
    else:
        assert response.location == "/oidc"
        with client.session_transaction() as session:
            assert session["login_next"] == expected
