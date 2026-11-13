from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
import re
import smtplib
import socket

from aiosmtpd.controller import Controller

from portal.calendar_feed import fold_line, ical_text
from portal.core import now
from portal.lesson_reminders import deliver_next_reminder, enqueue_reminders, weekly_window
from test_portal import app, database, users, lesson_record, post, sign_in


def calendar_link(client):
    assert post(client, "/calendar/renew").status_code == 303
    return re.search(r'id="calendar-link" type="url" value="([^"]+)"', client.get("/calendar").get_data(as_text=True))[1]


def test_calendar_feed_is_private_scoped_current_and_revocable(app, database, users):
    first = lesson_record(database, users, "student", now() + timedelta(days=1), title="Algebra, equations; graphs")
    sibling = lesson_record(database, users, "sibling", now() + timedelta(days=2))
    outsider = lesson_record(database, users, "other_student", now() + timedelta(days=3))
    parent = sign_in(app, users["parent"])
    link = calendar_link(parent)
    response = app.test_client().get(link)
    assert response.status_code == 200 and response.mimetype == "text/calendar"
    text = response.get_data(as_text=True)
    assert f"lesson-{first['_id']}@lesson-desk" in text and f"lesson-{sibling['_id']}@lesson-desk" in text
    assert str(outsider["_id"]) not in text
    assert "Algebra\\, equations\\; graphs" in text
    assert "parent_feedback" not in text and "rate_cents" not in text
    database.lessons.update_one({"_id": first["_id"]}, {"$set": {"status": "cancelled"}})
    assert "STATUS:CANCELLED" in app.test_client().get(link).get_data(as_text=True)
    new_link = calendar_link(parent)
    assert app.test_client().get(link).status_code == 404
    assert app.test_client().get(new_link).status_code == 200
    assert post(parent, "/calendar/revoke").status_code == 303
    assert app.test_client().get(new_link).status_code == 404
    student_link = calendar_link(sign_in(app, users["student"]))
    student_feed = app.test_client().get(student_link).get_data(as_text=True)
    assert str(first["_id"]) in student_feed and str(sibling["_id"]) not in student_feed
    database.users.update_one({"_id": users["student"]["_id"]}, {"$set": {"active": False}})
    assert app.test_client().get(student_link).status_code == 404


def test_ical_lines_escape_and_fold_unicode_without_splitting_characters():
    line = "SUMMARY:" + "数学课程" * 40
    folded = fold_line(line)
    assert all(len(part.encode("utf-8")) <= 75 for part in folded.split("\r\n"))
    assert folded.replace("\r\n ", "") == line
    assert ical_text("a\\b;c,d\r\nx") == "a\\\\b\\;c\\,d\\nx"


def configure_mail(app):
    app.config.update(SMTP_HOST="smtp.example.test", MAIL_FROM="lessons@example.test")


def test_24_hour_reminders_are_deduplicated_and_follow_current_family_links(app, database, users, monkeypatch):
    configure_mail(app)
    start = now() + timedelta(hours=2)
    lesson = lesson_record(database, users, "student", start)
    lesson_record(database, users, "sibling", start + timedelta(days=2))
    sent = []
    monkeypatch.setattr("portal.lesson_reminders.send_reminder", lambda job, contents: sent.append((job, contents)))
    with app.app_context():
        enqueue_reminders()
        enqueue_reminders()
        assert database.lesson_reminder_deliveries.count_documents({}) == 3
        # A parent unlink before delivery must not leak the child's schedule.
        database.users.update_one({"_id": users["student"]["_id"]}, {"$set": {"parent_id": users["other_parent"]["_id"]}})
        while deliver_next_reminder():
            pass
        assert len(sent) == 2
        assert {contents[0] for _, contents in sent} == {users["tutor"]["email"], users["student"]["email"]}
        enqueue_reminders()
        while deliver_next_reminder():
            pass
        assert len(sent) == 3 and sent[-1][1][0] == users["other_parent"]["email"]
        assert all("starts within 24 hours" in contents[2] for _, contents in sent)
        assert all(str(lesson["_id"]) in contents[2] for _, contents in sent)


def test_reminders_skip_cancelled_or_rescheduled_lessons(app, database, users, monkeypatch):
    configure_mail(app)
    lesson = lesson_record(database, users, "student", now() + timedelta(hours=2))
    sent = []
    monkeypatch.setattr("portal.lesson_reminders.send_reminder", lambda *args: sent.append(args))
    with app.app_context():
        enqueue_reminders()
        database.lessons.update_one({"_id": lesson["_id"]}, {"$set": {"start_at": now() + timedelta(days=2)}})
        while deliver_next_reminder():
            pass
        assert not sent
        assert database.lesson_reminder_deliveries.count_documents({"state": "skipped"}) == 3
        database.lessons.update_one({"_id": lesson["_id"]}, {"$set": {"start_at": now() + timedelta(hours=3), "end_at": now() + timedelta(hours=4)}})
        enqueue_reminders()
        database.lessons.update_one({"_id": lesson["_id"]}, {"$set": {"status": "cancelled"}})
        while deliver_next_reminder():
            pass
        assert not sent


def test_weekly_summary_uses_sunday_evening_in_app_timezone_and_sends_once(app, database, users, monkeypatch):
    configure_mail(app)
    sunday_evening = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)  # 18:00 Shanghai
    lesson_record(database, users, "student", sunday_evening + timedelta(days=2))
    sent = []
    monkeypatch.setattr("portal.lesson_reminders.now", lambda: sunday_evening)
    monkeypatch.setattr("portal.lesson_reminders.send_reminder", lambda job, contents: sent.append((job, contents)))
    with app.app_context():
        assert weekly_window(sunday_evening - timedelta(minutes=1)) is None
        assert weekly_window(sunday_evening)[0].isoformat() == "2026-11-16"
        enqueue_reminders(sunday_evening)
        enqueue_reminders(sunday_evening + timedelta(minutes=30))
        assert database.lesson_reminder_deliveries.count_documents({"kind": "week"}) == 3
        while deliver_next_reminder():
            pass
        enqueue_reminders(sunday_evening + timedelta(hours=1))
        assert not deliver_next_reminder()
        assert len(sent) == 3 and all("coming week" in contents[1] for _, contents in sent)


def test_reminder_missing_configuration_and_retry_limit(app, database, users, monkeypatch):
    lesson_record(database, users, "student", now() + timedelta(hours=2))
    app.config.update(SMTP_HOST="", MAIL_FROM="")
    with app.app_context():
        enqueue_reminders()
        assert not deliver_next_reminder()
        assert not database.lesson_reminder_deliveries.count_documents({"attempts": {"$gt": 0}})
        configure_mail(app)
        def fail(*args):
            raise smtplib.SMTPAuthenticationError(535, b"Test failure")
        monkeypatch.setattr("portal.lesson_reminders.send_reminder", fail)
        for _ in range(3):
            database.lesson_reminder_deliveries.update_many({}, {"$set": {"next_at": now() - timedelta(minutes=1)}})
            while deliver_next_reminder():
                pass
        assert database.lesson_reminder_deliveries.count_documents({"state": "failed", "attempts": 3}) == 3
        assert not deliver_next_reminder()


def test_worker_sends_reminder_through_local_smtp(app, database, users):
    lesson_record(database, users, "student", now() + timedelta(hours=2))
    messages = []

    class Receiver:
        async def handle_DATA(self, server, session, envelope):
            messages.append(envelope.original_content)
            return "250 OK"

    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        port = connection.getsockname()[1]
    smtp = Controller(Receiver(), hostname="127.0.0.1", port=port)
    smtp.start()
    try:
        app.config.update(SMTP_HOST="127.0.0.1", SMTP_PORT=port, SMTP_SECURITY="none", MAIL_FROM="lessons@example.test")
        with app.app_context():
            enqueue_reminders()
            while deliver_next_reminder():
                pass
    finally:
        smtp.stop()
    parsed = [BytesParser(policy=policy.default).parsebytes(message) for message in messages]
    assert {message["To"] for message in parsed} == {users[role]["email"] for role in ("parent", "student", "tutor")}
    assert all("Lesson reminder:" in message["Subject"] for message in parsed)
    assert all("Asia/Shanghai" in message.get_content() for message in parsed)
    assert database.lesson_reminder_deliveries.count_documents({"state": "sent"}) == 3


def test_announcement_unread_badge_clears_on_view_and_reappears_on_edit(app, database, users):
    tutor = sign_in(app, users["tutor"])
    parent = sign_in(app, users["parent"])
    assert post(tutor, "/announcements/new", title="Unread notice", body="Please read.", important="yes", popup="yes").status_code == 303
    announcement = database.announcements.find_one({})
    page = parent.get("/lessons").get_data(as_text=True)
    assert 'data-announcement-count aria-label="1 unread announcements">1' in page
    assert "Mark as read" not in page
    detail = parent.get(f"/announcements/{announcement['_id']}").get_data(as_text=True)
    assert "Mark as read" not in detail and "data-announcement-count" not in detail
    assert b"data-announcement-popup" not in parent.get("/lessons").data
    assert post(tutor, f"/announcements/{announcement['_id']}/edit", title="Updated notice", body="Read again.").status_code == 303
    assert b"data-announcement-count" in parent.get("/lessons").data
    assert b"data-announcement-count" not in parent.get("/announcements").data
    assert b"data-announcement-count" in sign_in(app, users["other_parent"]).get("/lessons").data
