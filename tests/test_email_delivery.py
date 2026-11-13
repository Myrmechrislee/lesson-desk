from datetime import timedelta
import smtplib

from bson import ObjectId
from flask import Flask
import pytest

from portal.billing import deliver_next, send_invoice
from portal.core import day_label, money, now
from test_portal import app, database, users, lesson_record, post, sign_in


def create_invoice(app, database, users):
    tutor = sign_in(app, users["tutor"])
    lesson = lesson_record(database, users, "student", now() - timedelta(days=2))
    assert post(tutor, f"/lessons/{lesson['_id']}/outcome", status="completed", parent_feedback="Good progress").status_code == 303
    assert post(tutor, "/invoices", parent_id=str(users["parent"]["_id"])).status_code == 303
    return database.invoices.find_one({})


def test_failed_email_stops_after_three_attempts_until_admin_retries(app, database, users, monkeypatch):
    invoice = create_invoice(app, database, users)
    app.config.update(SMTP_HOST="smtp.example.test", MAIL_FROM="billing@example.test")
    calls = []

    def fail(row):
        calls.append(row["_id"])
        raise smtplib.SMTPDataError(451, b"Temporary service failure")

    monkeypatch.setattr("portal.billing.send_invoice", fail)
    for attempt in range(1, 4):
        database.invoices.update_one({"_id": invoice["_id"]}, {"$set": {"delivery.next_at": now() - timedelta(seconds=1)}})
        with app.app_context():
            assert deliver_next()
            assert not deliver_next()
        saved = database.invoices.find_one({"_id": invoice["_id"]})
        assert saved["delivery"]["attempts"] == attempt
        assert "Temporary service failure" in saved["delivery"]["reason"]
        assert saved["delivery"]["state"] == ("failed" if attempt == 3 else "queued")
    assert len(calls) == 3
    retry = f"/invoices/{invoice['_id']}/retry"
    for role in ("tutor", "parent", "student"):
        assert post(sign_in(app, users[role]), retry).status_code == 403
    tutor_page = sign_in(app, users["tutor"]).get("/invoices")
    assert b"Temporary service failure" in tutor_page.data and b"Waiting for an administrator" in tutor_page.data
    assert b"Retry email" not in tutor_page.data
    admin = sign_in(app, users["admin"])
    assert b"Retry email" in admin.get("/invoices").data
    assert post(admin, retry).status_code == 303
    monkeypatch.setattr("portal.billing.send_invoice", lambda row: calls.append(row["_id"]))
    with app.app_context():
        assert deliver_next()
        assert not deliver_next()
    saved = database.invoices.find_one({"_id": invoice["_id"]})
    assert saved["delivery"]["state"] == "sent" and saved["delivery"]["attempts"] == 1
    assert saved["delivery"]["total_attempts"] == 4
    assert saved["delivery"]["retried_by"] == users["admin"]["_id"]
    assert "reason" not in saved["delivery"]
    assert post(admin, retry).status_code == 409


def test_missing_mail_configuration_explains_delay_without_consuming_attempts(app, database, users):
    invoice = create_invoice(app, database, users)
    app.config.update(SMTP_HOST="", MAIL_FROM="")
    with app.app_context():
        assert not deliver_next()
    saved = database.invoices.find_one({"_id": invoice["_id"]})
    assert saved["delivery"]["state"] == "queued" and saved["delivery"]["attempts"] == 0
    assert "missing SMTP host and sender address" in saved["delivery"]["reason"]
    assert b"missing SMTP host and sender address" in sign_in(app, users["tutor"]).get("/invoices").data


def test_expired_third_worker_lease_stops_without_an_extra_send(app, database, users, monkeypatch):
    invoice = create_invoice(app, database, users)
    app.config.update(SMTP_HOST="smtp.example.test", MAIL_FROM="billing@example.test")
    database.invoices.update_one({"_id": invoice["_id"]}, {"$set": {
        "delivery.state": "sending", "delivery.attempts": 3, "delivery.lease": "expired",
        "delivery.lease_until": now() - timedelta(minutes=1),
    }})
    calls = []
    monkeypatch.setattr("portal.billing.send_invoice", lambda row: calls.append(row["_id"]))
    with app.app_context():
        assert not deliver_next()
    assert not calls
    assert database.invoices.find_one({"_id": invoice["_id"]})["delivery"]["state"] == "failed"


@pytest.mark.parametrize("security", ["none", "starttls", "ssl"])
@pytest.mark.parametrize("sender", ["portal@example.test", "Lesson Desk <portal@example.test>"])
def test_invoice_email_uses_configured_sender_and_themed_html(monkeypatch, security, sender):
    """Check the message and SMTP envelope without requiring a database or relay."""
    application = Flask("portal")
    application.config.update(
        MAIL_FROM=sender, APP_BASE_URL="https://lessons.example.test", AUTH0_DOMAIN="unit-test.auth0.com",
        SMTP_HOST="smtp.example.test", SMTP_PORT=587, SMTP_SECURITY=security,
        SMTP_USERNAME="relay-account@example.test", SMTP_PASSWORD="test-password",
    )
    application.jinja_env.filters.update(day_label=day_label, money=money)
    sent = {}

    class Relay:
        def __init__(self, *args, **kwargs):
            pass

        def starttls(self, **kwargs):
            sent["starttls"] = True

        def login(self, username, password):
            sent["login"] = username

        def send_message(self, message, **kwargs):
            sent.update(message=message, **kwargs)

        def close(self):
            sent["closed"] = True

    monkeypatch.setattr("portal.billing.smtplib.SMTP", Relay)
    monkeypatch.setattr("portal.billing.smtplib.SMTP_SSL", Relay)
    invoice = {
        "_id": ObjectId(), "number": "INV-2026-00001", "issued_at": now(),
        "recipient": {"name": "Parent <script>alert(1)</script>", "email": "parent@example.test"},
        "supplier": {"name": "Studio & Tutoring", "email": "tutor@example.test"},
        "due_on": "2026-12-01", "total_cents": 12550, "currency": "AUD",
    }
    with application.app_context():
        send_invoice(invoice)
    message = sent["message"]
    assert str(message["From"]) == sender
    assert sent["from_addr"] == "portal@example.test"
    assert sent["to_addrs"] == ["parent@example.test"]
    assert str(message["Reply-To"]) == sender
    assert sent["login"] == "relay-account@example.test" and sent["closed"]
    assert sent.get("starttls", False) == (security == "starttls")
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    for body in (plain, html):
        assert "INV-2026-00001" in body and "$125.50" in body and "01 Dec 2026" in body
        assert "https://lessons.example.test/invoices" in body
        assert f"https://lessons.example.test/invoices/{invoice['_id']}/pdf" in body
        assert "Sign in with your parent account" in body
        assert "attached" not in body
        assert "If you have any questions, please feel free to reach out" in body
    assert "#173a2f" in html and "#fffefa" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "Studio &amp; Tutoring" in html
    assert list(message.iter_attachments()) == []
    assert message.get_content_type() == "multipart/alternative"
