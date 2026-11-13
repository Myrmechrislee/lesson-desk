from datetime import timedelta
from io import BytesIO

from bson import ObjectId
from PIL import Image

from portal.core import now
from test_portal import app, csrf, database, lesson_record, post, sign_in, users


def comment_url(kind, target):
    return f"/comments/{kind}/{target['_id']}"


def announcement(database, users):
    row = {"_id": ObjectId(), "title": "Term news", "body": "Welcome", "created_by": users["admin"]["_id"], "created_at": now()}
    database.announcements.insert_one(row)
    return row


def photo():
    content = BytesIO()
    Image.new("RGB", (5, 5), "red").save(content, "PNG")
    content.seek(0)
    return content, "image.png"


def test_lesson_comment_notifies_tutor_replies_and_author_edits(app, database, users):
    lesson = lesson_record(database, users, "student", now() + timedelta(days=1))
    parent = sign_in(app, users["parent"])
    tutor = sign_in(app, users["tutor"])
    url = comment_url("lesson", lesson)
    assert post(parent, url, body="**Could we revise fractions?**").status_code == 303
    comment = database.comments.find_one({})
    notice = database.notifications.find_one({"user_id": users["tutor"]["_id"]})
    assert notice["comment_id"] == comment["_id"]
    assert b"<strong>Could we revise fractions?</strong>" in tutor.get(f"/lessons/{lesson['_id']}").data
    assert post(tutor, url, body="Yes, next lesson.", reply_to=str(comment["_id"])).status_code == 303
    assert database.comments.count_documents({"root_id": comment["_id"]}) == 1
    assert database.notifications.count_documents({"user_id": users["parent"]["_id"]}) == 1
    edit = f"{url}/{comment['_id']}/edit"
    assert post(tutor, edit, body="Tutor cannot edit parent").status_code == 403
    assert post(parent, edit, body="Fractions and decimals").status_code == 303
    assert database.comments.find_one({"_id": comment["_id"]})["edited_at"] is not None
    assert b"Edited " in parent.get(f"/lessons/{lesson['_id']}").data


def test_unrelated_family_cannot_comment_reply_or_read_lesson_image(app, database, users):
    lesson = lesson_record(database, users, "student", now())
    parent = sign_in(app, users["parent"])
    other = sign_in(app, users["other_parent"])
    url = comment_url("lesson", lesson)
    assert post(parent, url, body="Homework", images=photo()).status_code == 303
    comment = database.comments.find_one({})
    image = database.comment_images.find_one({})
    image_url = f"{url}/{comment['_id']}/images/{image['_id']}"
    assert parent.get(image_url).status_code == 200
    assert other.get(image_url).status_code == 404
    assert post(other, url, body="Not enrolled").status_code == 404
    assert post(other, url, body="Reply", reply_to=str(comment["_id"])).status_code == 404


def test_announcement_threads_and_images_are_private_to_family(app, database, users):
    item = announcement(database, users)
    parent = sign_in(app, users["parent"])
    student = sign_in(app, users["student"])
    other = sign_in(app, users["other_parent"])
    tutor = sign_in(app, users["tutor"])
    admin = sign_in(app, users["admin"])
    url = comment_url("announcement", item)
    assert post(parent, url, body="Private family question", images=photo()).status_code == 303
    private = database.comments.find_one({})
    private_image = database.comment_images.find_one({})
    assert post(student, url, body="Child question").status_code == 303
    assert post(other, url, body="Other family question").status_code == 303
    assert post(tutor, url, body="Private answer", reply_to=str(private["_id"])).status_code == 303
    detail_url = f"/announcements/{item['_id']}"
    family_page = parent.get(detail_url).data
    assert b"Private family question" in family_page
    assert b"Child question" in family_page
    assert b"Private answer" in family_page
    assert b"Other family question" not in family_page
    assert b"Private family question" not in other.get(detail_url).data
    assert b"Other family question" in tutor.get(detail_url).data
    assert b"Private family question" in admin.get(detail_url).data
    assert database.notifications.count_documents({"user_id": users["admin"]["_id"]}) == 4
    assert post(other, url, body="Guess a thread", reply_to=str(private["_id"])).status_code == 404
    image_url = f"{url}/{private['_id']}/images/{private_image['_id']}"
    assert parent.get(image_url).status_code == 200
    assert tutor.get(image_url).status_code == 200
    assert other.get(image_url).status_code == 404
    edit_url = f"{url}/{private['_id']}/edit"
    assert post(other, edit_url, body="Guess an edit").status_code == 404


def test_course_comments_use_topic_membership_and_notify_tutor(app, database, users):
    topic = database.enrollments.find_one({"student_id": users["student"]["_id"]})
    student = sign_in(app, users["student"])
    other = sign_in(app, users["other_student"])
    url = comment_url("course", topic)
    assert post(student, url, body="Could I see more examples?").status_code == 303
    assert database.notifications.count_documents({"user_id": users["tutor"]["_id"]}) == 1
    assert post(other, url, body="Wrong course").status_code == 404


def test_comment_images_are_validated_empty_comments_rejected_and_csrf_required(app, database, users):
    lesson = lesson_record(database, users, "student", now())
    parent = sign_in(app, users["parent"])
    url = comment_url("lesson", lesson)
    assert parent.post(url, data={"body": "Missing CSRF"}).status_code == 400
    assert post(parent, url, body=" ").status_code == 400
    assert post(parent, url, body="Too long" * 1000).status_code == 400
    assert post(parent, url, body="Bad image", images=(BytesIO(b"<script>bad</script>"), "image.png")).status_code == 400
    assert database.comments.count_documents({}) == 0
    assert post(parent, url, body="<script>alert(1)</script>").status_code == 303
    assert b"<script>alert(1)</script>" not in parent.get(f"/lessons/{lesson['_id']}").data


def test_notification_read_is_owner_scoped_and_rechecks_access(app, database, users):
    lesson = lesson_record(database, users, "student", now())
    parent = sign_in(app, users["parent"])
    tutor = sign_in(app, users["tutor"])
    other = sign_in(app, users["other_tutor"])
    assert post(parent, comment_url("lesson", lesson), body="Question").status_code == 303
    notice = database.notifications.find_one({})
    url = f"/notifications/{notice['_id']}/read"
    assert post(other, url).status_code == 404
    assert post(tutor, url).status_code == 303
    assert database.notifications.find_one({"_id": notice["_id"]})["read_at"] is not None
    database.lessons.update_one({"_id": lesson["_id"]}, {"$set": {"tutor_id": users["other_tutor"]["_id"]}})
    assert post(tutor, url).status_code == 404


def test_comment_edit_can_replace_images_but_not_delete_last_content(app, database, users):
    lesson = lesson_record(database, users, "student", now())
    parent = sign_in(app, users["parent"])
    url = comment_url("lesson", lesson)
    assert post(parent, url, images=photo()).status_code == 303
    comment = database.comments.find_one({})
    image = database.comment_images.find_one({})
    edit_url = f"{url}/{comment['_id']}/edit"
    assert post(parent, edit_url, remove_images=str(image["_id"])).status_code == 400
    assert post(parent, edit_url, body="Image removed", remove_images=str(image["_id"])).status_code == 303
    assert database.comment_images.count_documents({}) == 0


def test_announcement_privacy_rechecks_current_child_relationships(app, database, users):
    item = announcement(database, users)
    parent = sign_in(app, users["parent"])
    student = sign_in(app, users["student"])
    tutor = sign_in(app, users["tutor"])
    url = comment_url("announcement", item)
    assert post(student, url, body="Private student thread", images=photo()).status_code == 303
    comment = database.comments.find_one({})
    image = database.comment_images.find_one({})
    assert post(tutor, url, body="Private tutor reply", reply_to=str(comment["_id"])).status_code == 303
    database.users.update_one({"_id": users["student"]["_id"]}, {"$set": {"parent_id": users["other_parent"]["_id"]}})
    page = parent.get(f"/announcements/{item['_id']}").data
    assert b"Private student thread" not in page
    assert b"Private tutor reply" not in page
    assert parent.get(f"{url}/{comment['_id']}/images/{image['_id']}").status_code == 404
