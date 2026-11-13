from datetime import timedelta
from decimal import Decimal
from email.message import EmailMessage
from email.utils import format_datetime, parseaddr
from io import BytesIO
import re
import secrets
import smtplib
import ssl

from bson import Binary, ObjectId
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file, url_for
from pymongo import ReturnDocument

from .core import (
    FINAL_STATUSES, Problem, atomic, boundary, date_field, db, email_field, int_field, local_datetime, lock_accounts,
    day_label, money, now, object_id, people, requires, text_field, zone,
)
from .invoice_pdf import invoice_pdf

bp = Blueprint("billing", __name__)
INVOICE_STATUSES = {"unpaid": "Not paid", "paid": "Paid", "overdue": "Overdue"}
INVOICE_LIST_FIELDS = {"pdf": 0, "lines": 0, "supplier": 0, "edit_history": 0}
MAX_DELIVERY_ATTEMPTS = 3


def email_configuration_reason():
    missing = [label for key, label in (("SMTP_HOST", "SMTP host"), ("MAIL_FROM", "sender address"))
               if not current_app.config[key]]
    return "Email is not configured: missing " + " and ".join(missing) + "." if missing else None


def delivery_reason(invoice):
    delivery = invoice.get("delivery", {})
    state = delivery.get("state", "queued")
    if state == "sent":
        return ""
    if state == "sending":
        return "The email worker is sending this invoice."
    if state == "draft":
        return "Invoice updated. Choose Resend PDF when it is ready to email."
    if state == "failed":
        return delivery.get("reason") or "Automatic delivery stopped after three attempts. An administrator must retry it."
    return email_configuration_reason() or delivery.get("reason") or "Waiting for the email worker to send this invoice."


def email_error_reason(error):
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return "The mail server rejected the SMTP credentials. An administrator must check the email configuration."
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        replies = list(error.recipients.values())
        code, reply = replies[0] if replies else ("", "Recipient rejected")
        if isinstance(reply, bytes):
            reply = reply.decode("utf-8", errors="replace")
        return f"The mail server rejected the recipient ({code}): {' '.join(str(reply).split())[:300]}"
    if isinstance(error, smtplib.SMTPResponseException):
        reply = error.smtp_error
        if isinstance(reply, bytes):
            reply = reply.decode("utf-8", errors="replace")
        return f"Mail server error {error.smtp_code}: {' '.join(str(reply).split())[:300]}"
    if isinstance(error, TimeoutError):
        return "The connection to the mail server timed out."
    if isinstance(error, OSError):
        return "Could not connect to the mail server: " + (error.strerror or type(error).__name__)[:300]
    return "Email delivery failed: " + " ".join(str(error).split())[:300]


def valid_abn(value):
    if not re.fullmatch(r"[0-9]{11}", value):
        return False
    digits = list(map(int, value))
    digits[0] -= 1
    weights = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)
    return sum(digit * weight for digit, weight in zip(digits, weights)) % 89 == 0


@bp.get("/profile")
@requires("tutor")
def profile():
    return render_template("profile.html", profile=g.user.get("billing", {}))


@bp.post("/profile")
@requires("tutor")
def save_profile():
    abn = "".join(request.form.get("abn", "").split())
    if not valid_abn(abn):
        raise Problem("Enter an 11-digit ABN with valid check digits.")
    profile = {
        "name": text_field(request.form, "name", "a billing name", limit=100, required=True),
        "abn": abn,
        "email": email_field(request.form),
        "phone": text_field(request.form, "phone", "the phone number", limit=40),
        "address": text_field(request.form, "address", "the address", limit=300),
        "payment": text_field(request.form, "payment", "payment instructions", limit=600),
        "due_days": int_field(request.form, "due_days", "the number of days until payment is due", 0, 365),
    }
    result = db().users.update_one(
        {"_id": g.user["_id"], "role": "tutor", "active": True}, {"$set": {"billing": profile}},
    )
    if not result.matched_count:
        abort(403)
    flash("Billing profile saved.")
    return redirect(url_for("billing.profile"), code=303)


def uninvoiced(tutor_id):
    return {
        "tutor_id": tutor_id, "status": {"$in": FINAL_STATUSES}, "invoice_id": None,
        "cancellation_fee_pending": {"$ne": True},
    }


def generate(tutor_id, parent_id):
    if tutor_id == parent_id:
        raise Problem("Choose a parent account.")

    def create(transaction):
        database = db()
        lock_accounts(transaction, {tutor_id: "tutor", parent_id: "parent"})
        tutor = database.users.find_one({"_id": tutor_id}, {"billing": 1}, session=transaction)
        parent = database.users.find_one({"_id": parent_id}, {"name": 1, "email": 1}, session=transaction)
        supplier = tutor.get("billing")
        if not supplier:
            raise Problem("Save your ABN and contact details in your billing profile first.")

        lessons = list(database.lessons.find(
            {**uninvoiced(tutor_id), "billing_parent_id": parent_id},
            {"student_id": 1, "start_at": 1, "end_at": 1, "rate_cents": 1, "charge": 1, "topic": 1},
            session=transaction,
        ).sort([("start_at", 1), ("_id", 1)]))
        if not lessons:
            raise Problem("There are no finalized, uninvoiced lessons for this parent.", 409)
        student_ids = {lesson["student_id"] for lesson in lessons}
        names = {
            user["_id"]: user["name"]
            for user in database.users.find({"_id": {"$in": list(student_ids)}}, {"name": 1}, session=transaction)
        }
        issued_at = now()
        local_day = issued_at.astimezone(zone()).date()
        due_on = local_day + timedelta(days=supplier["due_days"])
        sequence = database.counters.find_one_and_update(
            {"_id": f"invoice-{local_day.year}"}, {"$inc": {"value": 1}},
            upsert=True, return_document=ReturnDocument.AFTER, session=transaction,
        )["value"]
        lines = [{
            "lesson_id": lesson["_id"],
            "student": names[lesson["student_id"]],
            **({"topic": lesson["topic"]} if lesson.get("topic") else {}),
            "start_at": lesson["start_at"], "end_at": lesson["end_at"],
            "amount_cents": lesson["rate_cents"] if lesson["charge"] else 0,
        } for lesson in lessons]
        total = sum(line["amount_cents"] for line in lines)
        invoice = {
            "_id": ObjectId(), "number": f"INV-{local_day.year}-{sequence:05d}",
            "tutor_id": tutor_id, "parent_id": parent_id,
            "supplier": supplier, "recipient": {"name": parent["name"], "email": parent["email"]},
            "issued_at": issued_at, "due_on": due_on.isoformat(),
            "due_at": boundary(due_on + timedelta(days=1)),
            "currency": "AUD", "lines": lines, "total_cents": total,
            "status": "unpaid" if total else "paid",
            "delivery": {"state": "queued", "next_at": issued_at, "attempts": 0},
        }
        invoice["pdf"] = Binary(invoice_pdf(invoice))
        database.invoices.insert_one(invoice, session=transaction)
        database.lessons.update_many(
            {"_id": {"$in": [lesson["_id"] for lesson in lessons]}},
            {"$set": {"invoice_id": invoice["_id"]}}, session=transaction,
        )
        return invoice["_id"]

    return atomic(create)


def refresh_overdue(scope=None):
    return db().invoices.update_many(
        {**(scope or {}), "status": "unpaid", "due_at": {"$lte": now()}},
        {"$set": {"status": "overdue"}},
    ).modified_count


@bp.get("/invoices")
@requires("admin", "tutor", "parent")
def index():
    role = g.user["role"]
    scope = {} if role == "admin" else {f"{role}_id": g.user["_id"]}
    refresh_overdue(scope)
    invoices = list(db().invoices.find(scope, INVOICE_LIST_FIELDS).sort("issued_at", -1))
    for invoice in invoices:
        if role in {"admin", "tutor"}:
            invoice["delivery_reason"] = delivery_reason(invoice)
    groups = []
    recipients = {}
    if role == "tutor":
        groups = list(db().lessons.aggregate([
            {"$match": {**uninvoiced(g.user["_id"]), "billing_parent_id": {"$ne": None}}},
            {"$group": {"_id": "$billing_parent_id", "count": {"$sum": 1}}},
        ]))
        recipients = people(group["_id"] for group in groups)
        groups.sort(key=lambda group: recipients[group["_id"]]["name"].casefold())
    return render_template(
        "invoices.html", invoices=invoices, recipients=recipients, groups=groups, statuses=INVOICE_STATUSES,
        tutor_names=people(invoice["tutor_id"] for invoice in invoices) if role == "admin" else {},
    )


@bp.post("/invoices")
@requires("tutor")
def create():
    generate(g.user["_id"], object_id(request.form.get("parent_id")))
    flash("Invoice created and queued for email delivery.")
    return redirect(url_for("billing.index"), code=303)


@bp.get("/invoices/<invoice_id>/pdf")
@requires("admin", "tutor", "parent")
def download(invoice_id):
    invoice = db().invoices.find_one(
        {"_id": object_id(invoice_id), **({} if g.user["role"] == "admin" else {f"{g.user['role']}_id": g.user["_id"]})},
        {"number": 1, "pdf": 1},
    )
    if not invoice:
        abort(404)
    return send_file(
        BytesIO(invoice["pdf"]), mimetype="application/pdf", as_attachment=True,
        download_name=invoice["number"] + ".pdf",
    )


def editable_invoice(invoice_id):
    query = {"_id": object_id(invoice_id)}
    if g.user["role"] == "tutor":
        query["tutor_id"] = g.user["_id"]
    invoice = db().invoices.find_one(query)
    if not invoice:
        abort(404)
    return invoice


def invoice_revision_query(invoice):
    revision = invoice.get("revision", 0)
    return {"revision": revision} if revision else {"$or": [{"revision": 0}, {"revision": {"$exists": False}}]}


def invoice_amount(value, *, adjustment=False):
    pattern = r"-?[0-9]{1,5}(?:\.[0-9]{1,2})?" if adjustment else r"[0-9]{1,5}(?:\.[0-9]{1,2})?"
    if not re.fullmatch(pattern, value.strip()):
        raise Problem("Enter an amount in AUD with up to two decimal places (maximum 99,999.99 per line).")
    return int(Decimal(value.strip()) * 100)


def queue_invoice_delivery(invoice, timestamp):
    delivery = dict(invoice.get("delivery", {}))
    for key in ("lease", "lease_until", "sent_at", "last_error", "last_error_at"):
        delivery.pop(key, None)
    delivery.update(
        state="queued", attempts=0, next_at=timestamp,
        reason="A revised invoice was queued for delivery." if invoice.get("revision") else "Invoice queued for resending.",
        requested_by=g.user["_id"], requested_at=timestamp, message_id=secrets.token_hex(16),
    )
    return delivery


@bp.get("/invoices/<invoice_id>/edit")
@requires("admin", "tutor")
def edit(invoice_id):
    invoice = editable_invoice(invoice_id)
    return render_template("invoice_edit.html", invoice=invoice, statuses=INVOICE_STATUSES)


@bp.post("/invoices/<invoice_id>/edit")
@requires("admin", "tutor")
def save_edit(invoice_id):
    invoice = editable_invoice(invoice_id)
    revision = int_field(request.form, "revision", "the invoice revision", 0, 2_000_000_000)
    if revision != invoice.get("revision", 0):
        raise Problem("This invoice changed while you were editing it. Reload it and try again.", 409)
    if invoice.get("delivery", {}).get("state") == "sending":
        raise Problem("This invoice is being emailed. Wait for delivery to finish before editing it.", 409)
    action = request.form.get("action", "save")
    if action not in {"save", "send"}:
        raise Problem("Choose whether to save or resend the invoice.")
    if action == "send" and invoice.get("delivery", {}).get("state") == "failed" and g.user["role"] != "admin":
        raise Problem("An administrator must resume this stopped email delivery.", 403)
    due_on = date_field(request.form.get("due_on"))
    recipient = {
        "name": text_field(request.form, "recipient_name", "the recipient name", limit=100, required=True),
        "email": email_field(request.form, "recipient_email"),
    }
    supplier = dict(invoice["supplier"])
    abn = "".join(request.form.get("supplier_abn", "").split())
    if not valid_abn(abn):
        raise Problem("Enter an 11-digit ABN with valid check digits.")
    supplier.update({
        "name": text_field(request.form, "supplier_name", "a billing name", limit=100, required=True),
        "abn": abn, "email": email_field(request.form, "supplier_email"),
        "phone": text_field(request.form, "supplier_phone", "the phone number", limit=40),
        "address": text_field(request.form, "supplier_address", "the address", limit=300),
        "payment": text_field(request.form, "payment", "payment instructions", limit=600),
    })
    note = text_field(request.form, "note", "the invoice note", limit=1000)
    lines = []
    for index, original in enumerate(invoice["lines"]):
        prefix = f"line_{index}_"
        if not original.get("lesson_id") and request.form.get(prefix + "remove") == "yes":
            continue
        line = dict(original)
        line["amount_cents"] = invoice_amount(request.form.get(prefix + "amount", ""), adjustment=not original.get("lesson_id"))
        if original.get("lesson_id"):
            line["student"] = text_field(request.form, prefix + "student", "the student name", limit=100, required=True)
            line["topic"] = text_field(request.form, prefix + "topic", "the topic", limit=100)
            line["start_at"] = local_datetime(request.form.get(prefix + "start", ""))
            line["end_at"] = local_datetime(request.form.get(prefix + "end", ""))
            if line["end_at"] <= line["start_at"]:
                raise Problem("Each invoice line must end after its start time.")
        else:
            line["description"] = text_field(request.form, prefix + "description", "an adjustment description", limit=300, required=True)
        lines.append(line)
    adjustment = text_field(request.form, "adjustment_description", "an adjustment description", limit=300)
    adjustment_amount = request.form.get("adjustment_amount", "").strip()
    if adjustment or adjustment_amount:
        if not adjustment or not adjustment_amount:
            raise Problem("Enter both an adjustment description and amount, or leave both empty.")
        if len(lines) >= 500:
            raise Problem("An invoice can contain up to 500 lines.")
        lines.append({"description": adjustment, "amount_cents": invoice_amount(adjustment_amount, adjustment=True)})
    total = sum(line["amount_cents"] for line in lines)
    if total < 0:
        raise Problem("The invoice total cannot be negative.")
    status = request.form.get("status")
    if status not in INVOICE_STATUSES:
        raise Problem("Choose an invoice status.")
    due_at = boundary(due_on + timedelta(days=1))
    timestamp = now()
    if not total:
        status = "paid"
    elif status != "paid":
        status = "overdue" if due_at <= timestamp else "unpaid"
    changes = {
        "recipient": recipient, "supplier": supplier, "due_on": due_on.isoformat(), "due_at": due_at,
        "lines": lines, "total_cents": total, "status": status, "note": note,
        "revision": revision + 1, "edited_at": timestamp, "edited_by": g.user["_id"],
        "paid_at": (invoice.get("paid_at") or timestamp) if status == "paid" else None,
    }
    updated = {**invoice, **changes}
    changes["pdf"] = Binary(invoice_pdf(updated))
    delivery = dict(invoice.get("delivery", {}))
    if action == "send":
        delivery = queue_invoice_delivery(updated, timestamp)
    elif delivery.get("state") != "failed":
        delivery.update(state="draft", reason="Invoice updated. Choose Resend PDF when it is ready to email.")
        for key in ("lease", "lease_until", "next_at"):
            delivery.pop(key, None)
    changes["delivery"] = delivery
    history = {key: invoice[key] for key in (
        "recipient", "supplier", "due_on", "lines", "total_cents", "status", "note",
    ) if key in invoice}
    history.update(revision=revision, replaced_at=timestamp, replaced_by=g.user["_id"])
    result = db().invoices.update_one(
        {"_id": invoice["_id"], "delivery.state": {"$ne": "sending"}, **invoice_revision_query(invoice)},
        {"$set": changes, "$push": {"edit_history": history}},
    )
    if not result.matched_count:
        raise Problem("This invoice changed or began sending while you were editing it. Reload it and try again.", 409)
    flash("Invoice updated and queued for email delivery." if action == "send" else "Invoice updated. You can resend the revised PDF from invoice history.")
    return redirect(url_for("billing.index"), code=303)


@bp.post("/invoices/<invoice_id>/resend")
@requires("admin", "tutor")
def resend(invoice_id):
    invoice = editable_invoice(invoice_id)
    state = invoice.get("delivery", {}).get("state", "queued")
    if state in {"queued", "sending"}:
        raise Problem("This invoice is already queued or being emailed. Wait for delivery to finish.", 409)
    if state == "failed" and g.user["role"] != "admin":
        raise Problem("An administrator must resume this stopped email delivery.", 403)
    timestamp = now()
    result = db().invoices.update_one(
        {"_id": invoice["_id"], "delivery.state": state, **invoice_revision_query(invoice)},
        {"$set": {"delivery": queue_invoice_delivery(invoice, timestamp)}, "$inc": {"revision": 1}},
    )
    if not result.matched_count:
        raise Problem("This invoice changed. Reload it and try again.", 409)
    flash("Invoice queued for email delivery.")
    return redirect(url_for("billing.index"), code=303)


@bp.post("/invoices/<invoice_id>/status")
@requires("tutor")
def set_status(invoice_id):
    status = request.form.get("status")
    if status not in INVOICE_STATUSES:
        raise Problem("Choose an invoice status.")
    query = {"_id": object_id(invoice_id), "tutor_id": g.user["_id"]}
    invoice = db().invoices.find_one(query, {"due_at": 1, "total_cents": 1, "revision": 1})
    if not invoice:
        abort(404)
    if not invoice["total_cents"]:
        status = "paid"
    elif status == "unpaid" and invoice["due_at"] <= now():
        status = "overdue"
    result = db().invoices.update_one(
        {**query, **invoice_revision_query(invoice)},
        {"$set": {"status": status, "paid_at": now() if status == "paid" else None}, "$inc": {"revision": 1}},
    )
    if not result.matched_count:
        raise Problem("This invoice changed. Reload it and try again.", 409)
    flash("Invoice status saved.")
    return redirect(url_for("billing.index"), code=303)


@bp.post("/invoices/<invoice_id>/retry")
@requires("admin")
def retry_delivery(invoice_id):
    result = db().invoices.update_one(
        {"_id": object_id(invoice_id), "delivery.state": "failed"},
        {"$set": {"delivery.state": "queued", "delivery.attempts": 0, "delivery.next_at": now(),
                  "delivery.reason": "An administrator requested another delivery attempt.",
                  "delivery.retried_by": g.user["_id"], "delivery.retried_at": now()},
         "$unset": {"delivery.lease": "", "delivery.lease_until": ""}, "$inc": {"revision": 1}},
    )
    if not result.matched_count:
        if not db().invoices.find_one({"_id": object_id(invoice_id)}, {"_id": 1}):
            abort(404)
        raise Problem("Only email deliveries that have stopped can be retried.", 409)
    flash("Email delivery resumed. It will stop again after three unsuccessful attempts.")
    return redirect(url_for("billing.index"), code=303)


def send_invoice(invoice):
    config = current_app.config
    sender = config["MAIL_FROM"].strip()
    sender_address = parseaddr(sender)[1]
    website_url = config["APP_BASE_URL"] + "/invoices"
    download_url = f"{website_url}/{invoice['_id']}/pdf"
    message = EmailMessage()
    message["From"] = sender
    message["To"] = invoice["recipient"]["email"]
    message["Reply-To"] = sender
    message["Date"] = format_datetime(invoice.get("delivery", {}).get("requested_at") or invoice["issued_at"])
    delivery_id = invoice.get("delivery", {}).get("message_id", "original")
    message["Message-ID"] = f"<invoice-{invoice['_id']}-{delivery_id}@{config['AUTH0_DOMAIN']}>"
    message["Subject"] = f"Lesson invoice {invoice['number']}"
    message.set_content(
        f"Hello {invoice['recipient']['name']},\n\n"
        f"Your lesson invoice {invoice['number']} is ready.\n\n"
        f"Total: {money(invoice['total_cents'])} {invoice['currency']}\n"
        f"Payment due: {day_label(invoice['due_on'])}\n\n"
        f"Download invoice PDF: {download_url}\n"
        "Sign in with your parent account to download your invoice.\n\n"
        f"For more information, please visit the website: {website_url}\n"
        "If you have any questions, please feel free to reach out by replying to this email.\n\n"
        f"{invoice['supplier']['name']}\n"
    )
    message.add_alternative(render_template(
        "email/invoice.html", invoice=invoice, website_url=website_url, download_url=download_url,
    ), subtype="html")

    context = ssl.create_default_context()
    host, port = config["SMTP_HOST"], config["SMTP_PORT"]
    connection = (
        smtplib.SMTP_SSL(host, port, context=context, timeout=20)
        if config["SMTP_SECURITY"] == "ssl" else smtplib.SMTP(host, port, timeout=20)
    )
    try:
        if config["SMTP_SECURITY"] == "starttls":
            connection.starttls(context=context)
        if config["SMTP_USERNAME"]:
            connection.login(config["SMTP_USERNAME"], config["SMTP_PASSWORD"])
        connection.send_message(message, from_addr=sender_address,
                                to_addrs=[invoice["recipient"]["email"]])
    finally:
        # DATA acceptance determines delivery. A failed QUIT must not cause a resend.
        connection.close()


def deliver_next():
    timestamp = now()
    lease = secrets.token_hex(16)
    invoices = db().invoices
    invoices.update_many({
        "delivery.attempts": {"$gte": MAX_DELIVERY_ATTEMPTS},
        "$or": [{"delivery.state": "queued"},
                {"delivery.state": "sending", "delivery.lease_until": {"$lte": timestamp}}],
    }, [{"$set": {"delivery.state": "failed", "delivery.reason": {"$ifNull": [
        "$delivery.last_error", "Email delivery could not be confirmed after three attempts.",
    ]}}}])
    configuration_reason = email_configuration_reason()
    if configuration_reason:
        invoices.update_many({"delivery.state": "queued"}, {"$set": {"delivery.reason": configuration_reason}})
        return False
    invoice = invoices.find_one_and_update(
        {"delivery.attempts": {"$lt": MAX_DELIVERY_ATTEMPTS}, "$or": [
            {"delivery.state": "queued", "delivery.next_at": {"$lte": timestamp}},
            {"delivery.state": "sending", "delivery.lease_until": {"$lte": timestamp}},
        ]},
        {"$set": {"delivery.state": "sending", "delivery.lease": lease,
                  "delivery.lease_until": timestamp + timedelta(minutes=5)},
         "$inc": {"delivery.attempts": 1, "delivery.total_attempts": 1}},
        sort=[("delivery.next_at", 1)],
        projection={"number": 1, "delivery": 1, "recipient": 1, "supplier": 1, "issued_at": 1,
                    "total_cents": 1, "currency": 1, "due_on": 1},
        return_document=ReturnDocument.AFTER,
    )
    if not invoice:
        return False

    unset = {"delivery.lease": "", "delivery.lease_until": ""}
    try:
        send_invoice(invoice)
    except (OSError, smtplib.SMTPException, ValueError) as error:
        attempts = invoice["delivery"]["attempts"]
        delay = min(3600, 30 * (2 ** min(8, attempts - 1)))
        stopped = attempts >= MAX_DELIVERY_ATTEMPTS
        reason = email_error_reason(error)
        update = {"delivery.state": "failed" if stopped else "queued", "delivery.reason": reason,
                  "delivery.last_error": reason, "delivery.last_error_at": now()}
        if stopped:
            unset["delivery.next_at"] = ""
        else:
            update["delivery.next_at"] = now() + timedelta(seconds=delay)
        current_app.logger.warning("Invoice %s delivery %s (%s).", invoice["number"],
                                   "stopped" if stopped else "will retry", type(error).__name__)
    else:
        update = {"delivery.state": "sent", "delivery.sent_at": now()}
        unset.update({"delivery.reason": "", "delivery.next_at": ""})
    invoices.update_one(
        {"_id": invoice["_id"], "delivery.lease": lease},
        {"$set": update, "$unset": unset},
    )
    return True
