import pytest

from test_portal import app, database, users, post, sign_in


def test_staff_manage_shared_announcements_and_parents_read_them(app, database, users):
    admin = sign_in(app, users["admin"])
    tutor = sign_in(app, users["tutor"])
    parent = sign_in(app, users["parent"])
    assert post(admin, "/announcements/new", title="Term dates", body="Lessons resume on Monday.").status_code == 303
    announcement = database.announcements.find_one({})
    edit_path = f"/announcements/{announcement['_id']}/edit"
    assert b"Lessons resume on Monday." in parent.get("/announcements").data
    assert b"Term dates" in parent.get("/lessons").data
    assert b"Add announcement" not in parent.get("/announcements").data
    assert parent.get(edit_path).status_code == 403
    assert post(parent, edit_path, title="Forbidden", body="Forbidden").status_code == 403
    assert post(tutor, edit_path, title="Updated dates", body="Lessons resume on Tuesday.").status_code == 303
    updated = database.announcements.find_one({})
    assert updated["created_by"] == users["admin"]["_id"]
    assert updated["updated_by"] == users["tutor"]["_id"]
    assert b"Lessons resume on Tuesday." in parent.get("/announcements").data
    assert post(tutor, f"/announcements/{announcement['_id']}/remove").status_code == 303
    assert database.announcements.count_documents({}) == 0


@pytest.mark.parametrize("role", ["parent", "student"])
def test_announcements_reject_family_mutations_and_validate_content(app, database, users, role):
    client = sign_in(app, users[role])
    assert post(client, "/announcements/new", title="No", body="No").status_code == 403
    assert client.get("/announcements").status_code == 200
    tutor = sign_in(app, users["tutor"])
    assert post(tutor, "/announcements/new", title=" ", body="Missing title").status_code == 400
    assert post(tutor, "/announcements/new", title="Too long", body="x" * 6001).status_code == 400
    assert database.announcements.count_documents({}) == 0
    assert post(tutor, "/announcements/new", title="<script>alert(1)</script>", body="<img src=x>").status_code == 303
    page = sign_in(app, users["parent"]).get("/announcements")
    assert b"&lt;script&gt;" in page.data
    assert b"<img src=x>" not in page.data
