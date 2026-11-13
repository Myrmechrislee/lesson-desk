from datetime import timedelta

import pytest

from portal.billing import deliver_next
from portal.core import now
from test_email_delivery import create_invoice
from test_portal import app, database, users, post, sign_in


def edit_data(invoice, **changes):
    data = {
        "revision": str(invoice.get("revision", 0)), "recipient_name": invoice["recipient"]["name"],
        "recipient_email": invoice["recipient"]["email"], "due_on": invoice["due_on"],
        "status": invoice["status"], "payment": invoice["supplier"].get("payment", ""),
        "supplier_name": invoice["supplier"]["name"], "supplier_abn": invoice["supplier"]["abn"],
        "supplier_email": invoice["supplier"]["email"], "supplier_phone": invoice["supplier"].get("phone", ""),
        "supplier_address": invoice["supplier"].get("address", ""),
        "note": invoice.get("note", ""), "action": "save",
    }
    # The fixture uses Asia/Shanghai; use a constant offset without depending on
    # the host timezone when populating the HTML datetime-local fields.
    for index, line in enumerate(invoice["lines"]):
        prefix = f"line_{index}_"
        data[prefix + "amount"] = f"{line['amount_cents'] / 100:.2f}"
        if line.get("lesson_id"):
            data.update({
                prefix + "student": line["student"], prefix + "topic": line.get("topic", ""),
                prefix + "start": (line["start_at"] + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M"),
                prefix + "end": (line["end_at"] + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M"),
            })
        else:
            data[prefix + "description"] = line["description"]
    return {**data, **changes}


def test_edit_preserves_lesson_links_regenerates_pdf_and_keeps_history(app, database, users):
    invoice = create_invoice(app, database, users)
    tutor = sign_in(app, users["tutor"])
    path = f"/invoices/{invoice['_id']}/edit"
    assert tutor.get(path).status_code == 200
    data = edit_data(invoice, recipient_name="Updated parent", recipient_email="corrected@example.test",
                     line_0_amount="75.25", note="Corrected lesson rate", payment="Updated bank details",
                     adjustment_description="Lesson credit", adjustment_amount="-5.25", due_on="2099-01-01")
    assert post(tutor, path, **data).status_code == 303
    saved = database.invoices.find_one({"_id": invoice["_id"]})
    assert saved["total_cents"] == 7000
    assert saved["recipient"] == {"name": "Updated parent", "email": "corrected@example.test"}
    assert saved["lines"][0]["lesson_id"] == invoice["lines"][0]["lesson_id"]
    assert saved["lines"][1] == {"description": "Lesson credit", "amount_cents": -525}
    assert saved["pdf"].startswith(b"%PDF") and saved["pdf"] != invoice["pdf"]
    assert saved["delivery"]["state"] == "draft"
    assert saved["revision"] == 1 and saved["edited_by"] == users["tutor"]["_id"]
    assert saved["edit_history"][0]["lines"] == invoice["lines"]
    assert saved["edit_history"][0]["total_cents"] == 6500
    assert database.lessons.find_one({"_id": invoice["lines"][0]["lesson_id"]})["invoice_id"] == invoice["_id"]
    assert post(tutor, "/invoices", parent_id=str(users["parent"]["_id"])).status_code == 409
    assert post(tutor, path, **data).status_code == 409
    parent = sign_in(app, users["parent"])
    assert parent.get(f"/invoices/{invoice['_id']}/pdf").data == saved["pdf"]
    assert b"Resend PDF" not in parent.get("/invoices").data


def test_resend_keeps_current_pdf_download_and_cannot_queue_duplicate(app, database, users, monkeypatch):
    invoice = create_invoice(app, database, users)
    tutor = sign_in(app, users["tutor"])
    path = f"/invoices/{invoice['_id']}"
    assert post(tutor, path + "/edit", **edit_data(invoice, line_0_amount="50", action="send")).status_code == 303
    saved = database.invoices.find_one({"_id": invoice["_id"]})
    assert saved["delivery"]["state"] == "queued"
    first_message_id = saved["delivery"]["message_id"]
    assert post(tutor, path + "/resend").status_code == 409
    calls = []
    app.config.update(SMTP_HOST="smtp.example.test", MAIL_FROM="billing@example.test")
    monkeypatch.setattr("portal.billing.send_invoice", lambda row: calls.append(row))
    with app.app_context():
        assert deliver_next()
        assert not deliver_next()
    assert len(calls) == 1 and calls[0]["_id"] == saved["_id"]
    assert "pdf" not in calls[0]
    assert sign_in(app, users["parent"]).get(path + "/pdf").data == saved["pdf"]
    assert post(tutor, path + "/resend").status_code == 303
    queued = database.invoices.find_one({"_id": invoice["_id"]})
    assert queued["delivery"]["message_id"] != first_message_id
    assert queued["delivery"]["attempts"] == 0
    assert queued["delivery"]["requested_by"] == users["tutor"]["_id"]
    assert queued["pdf"] == saved["pdf"]
    assert post(tutor, path + "/resend").status_code == 409


def test_only_invoice_staff_can_edit_or_resend(app, database, users):
    invoice = create_invoice(app, database, users)
    path = f"/invoices/{invoice['_id']}"
    for role in ("parent", "student"):
        client = sign_in(app, users[role])
        assert client.get(path + "/edit").status_code == 403
        assert post(client, path + "/edit", **edit_data(invoice)).status_code == 403
        assert post(client, path + "/resend").status_code == 403
    other_tutor = sign_in(app, users["other_tutor"])
    assert other_tutor.get(path + "/edit").status_code == 404
    assert post(other_tutor, path + "/edit", **edit_data(invoice)).status_code == 404
    assert post(other_tutor, path + "/resend").status_code == 404
    admin = sign_in(app, users["admin"])
    assert admin.get(path + "/edit").status_code == 200
    assert post(admin, path + "/edit", **edit_data(invoice, note="Reviewed by administrator")).status_code == 303


@pytest.mark.parametrize("changes", [
    {"line_0_amount": "NaN"}, {"line_0_amount": "-1"}, {"line_0_amount": "999999"},
    {"recipient_email": "invalid"}, {"due_on": "not-a-date"}, {"supplier_abn": "123"},
    {"adjustment_description": "Credit", "adjustment_amount": "-100"},
    {"adjustment_description": "Credit"}, {"status": "unknown"},
])
def test_invalid_edits_leave_invoice_untouched(app, database, users, changes):
    invoice = create_invoice(app, database, users)
    tutor = sign_in(app, users["tutor"])
    assert post(tutor, f"/invoices/{invoice['_id']}/edit", **edit_data(invoice, **changes)).status_code == 400
    assert database.invoices.find_one({"_id": invoice["_id"]}) == invoice


def test_sending_lease_prevents_edit_and_edit_cannot_erase_new_lease(app, database, users, monkeypatch):
    invoice = create_invoice(app, database, users)
    tutor = sign_in(app, users["tutor"])
    path = f"/invoices/{invoice['_id']}"
    database.invoices.update_one({"_id": invoice["_id"]}, {"$set": {"delivery.state": "sending"}})
    assert post(tutor, path + "/edit", **edit_data(invoice)).status_code == 409
    assert post(tutor, path + "/resend").status_code == 409
    database.invoices.update_one({"_id": invoice["_id"]}, {"$set": {"delivery.state": "queued"}})
    from portal.invoice_pdf import invoice_pdf

    def claim_during_pdf_render(row):
        database.invoices.update_one({"_id": invoice["_id"]}, {"$set": {"delivery.state": "sending", "delivery.lease": "claimed"}})
        return invoice_pdf(row)

    monkeypatch.setattr("portal.billing.invoice_pdf", claim_during_pdf_render)
    assert post(tutor, path + "/edit", **edit_data(invoice, note="Must not be saved")).status_code == 409
    saved = database.invoices.find_one({"_id": invoice["_id"]})
    assert saved["delivery"]["lease"] == "claimed" and "note" not in saved


def test_stopped_delivery_keeps_admin_retry_requirement_after_edit(app, database, users):
    invoice = create_invoice(app, database, users)
    database.invoices.update_one({"_id": invoice["_id"]}, {"$set": {"delivery.state": "failed", "delivery.attempts": 3}})
    tutor = sign_in(app, users["tutor"])
    path = f"/invoices/{invoice['_id']}"
    assert post(tutor, path + "/edit", **edit_data(invoice, action="send")).status_code == 403
    assert post(tutor, path + "/edit", **edit_data(invoice, note="Corrected" )).status_code == 303
    assert database.invoices.find_one({"_id": invoice["_id"]})["delivery"]["state"] == "failed"
    assert post(tutor, path + "/resend").status_code == 403
    assert post(sign_in(app, users["admin"]), path + "/resend").status_code == 303


def test_adjustments_can_be_changed_removed_and_zero_total_is_paid(app, database, users):
    invoice = create_invoice(app, database, users)
    tutor = sign_in(app, users["tutor"])
    path = f"/invoices/{invoice['_id']}/edit"
    assert post(tutor, path, **edit_data(invoice, adjustment_description="Full credit", adjustment_amount="-65")).status_code == 303
    saved = database.invoices.find_one({"_id": invoice["_id"]})
    assert saved["total_cents"] == 0 and saved["status"] == "paid"
    assert post(tutor, path, **edit_data(saved, line_1_remove="yes", status="unpaid")).status_code == 303
    saved = database.invoices.find_one({"_id": invoice["_id"]})
    assert len(saved["lines"]) == 1 and saved["total_cents"] == 6500 and saved["status"] == "unpaid"
