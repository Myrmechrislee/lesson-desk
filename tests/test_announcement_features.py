from datetime import timedelta
from io import BytesIO

from portal.core import now, stamp
from test_portal import app, database, users, csrf, post, sign_in


def test_announcement_upload_download_edit_and_removal(app, database, users):
    admin = sign_in(app, users["admin"])
    document = b"%PDF-1.4\nAnnouncement resource\n"
    response = post(admin, "/announcements/new", title="Term guide", body="Please read the attached guide.",
                    files=(BytesIO(document), "term-guide.pdf"))
    assert response.status_code == 303
    announcement = database.announcements.find_one({})
    file = database.announcement_files.find_one({"announcement_id": announcement["_id"]})
    assert file["data"] == document
    parent = sign_in(app, users["parent"])
    path = f"/announcements/{announcement['_id']}"
    download = path + f"/files/{file['_id']}"
    assert b"term-guide.pdf" in parent.get(path).data
    response = parent.get(download)
    assert response.data == document and "attachment" in response.headers["Content-Disposition"]
    assert sign_in(app, users["student"]).get(download).status_code == 200
    assert post(parent, path + "/edit", title="Forbidden", body="Forbidden").status_code == 403
    assert post(admin, path + "/edit", title="Updated guide", body="New guide.", remove_files=str(file["_id"]),
                files=(BytesIO(b"New term notes"), "notes.txt")).status_code == 303
    assert parent.get(download).status_code == 404
    assert database.announcement_files.count_documents({"announcement_id": announcement["_id"]}) == 1
    assert post(admin, path + "/remove").status_code == 303
    assert database.announcement_files.count_documents({}) == 0


def test_banner_is_scheduled_and_contains_only_title_and_read_more(app, database, users):
    admin = sign_in(app, users["admin"])
    with app.app_context():
        start, end = stamp(now() - timedelta(hours=1), "input"), stamp(now() + timedelta(hours=1), "input")
    assert post(admin, "/announcements/new", title="Term update", body="Full announcement text only on detail.",
                banner="yes", banner_start=start, banner_end=end).status_code == 303
    announcement = database.announcements.find_one({})
    parent = sign_in(app, users["parent"])
    page = parent.get("/homework").get_data(as_text=True)
    banner = page.split('class="announcement-banner"', 1)[1].split("</aside>", 1)[0]
    assert "Term update" in banner and "Read more" in banner
    assert "Full announcement text" not in banner
    assert b"Full announcement text" in parent.get(f"/announcements/{announcement['_id']}").data
    database.announcements.update_one({"_id": announcement["_id"]}, {"$set": {"banner_start": now() + timedelta(minutes=30)}})
    assert b'class="announcement-banner"' not in parent.get("/homework").data
    database.announcements.update_one({"_id": announcement["_id"]}, {"$set": {
        "banner_start": now() - timedelta(days=2), "banner_end": now() - timedelta(days=1),
    }})
    assert b'class="announcement-banner"' not in parent.get("/homework").data
    assert post(admin, "/announcements/new", title="Bad schedule", body="Text", banner="yes",
                banner_start=end, banner_end=start).status_code == 400


def test_important_popups_are_acknowledged_per_parent_and_version(app, database, users):
    admin = sign_in(app, users["admin"])
    assert post(admin, "/announcements/new", title="New policy", body="Please read this important update.",
                important="yes", popup="yes").status_code == 303
    announcement = database.announcements.find_one({})
    parent = sign_in(app, users["parent"])
    other_parent = sign_in(app, users["other_parent"])
    assert b"data-announcement-popup" in parent.get("/homework").data
    assert b"data-announcement-popup" not in sign_in(app, users["tutor"]).get("/homework").data
    path = f"/announcements/{announcement['_id']}"
    assert post(parent, path + "/acknowledge", version="stale").status_code == 409
    assert post(parent, path + "/acknowledge", version=announcement["popup_version"]).status_code == 303
    assert b"data-announcement-popup" not in parent.get("/homework").data
    assert b"data-announcement-popup" in other_parent.get("/homework").data
    # Acknowledgement persists across browser sessions and sign-ins.
    assert b"data-announcement-popup" not in sign_in(app, users["parent"]).get("/lessons").data
    assert post(admin, path + "/edit", title="Revised policy", body="Updated policy text.", important="yes", popup="yes").status_code == 303
    assert b"data-announcement-popup" in parent.get("/homework").data
    assert post(parent, path + "/acknowledge", version=announcement["popup_version"]).status_code == 409
    updated = database.announcements.find_one({"_id": announcement["_id"]})
    response = parent.post(path + "/acknowledge", data={"csrf_token": csrf(parent), "version": updated["popup_version"]},
                           headers={"Accept": "application/json"})
    assert response.status_code == 204
    assert b"data-announcement-popup" not in parent.get("/homework").data


def test_announcement_attachments_are_validated_and_scoped_to_the_post(app, database, users):
    tutor = sign_in(app, users["tutor"])
    assert post(tutor, "/announcements/new", title="Invalid file", body="Text", files=(BytesIO(b"Not a PDF"), "bad.pdf")).status_code == 400
    assert post(tutor, "/announcements/new", title="Invalid pop-up", body="Text", popup="yes").status_code == 400
    assert database.announcements.count_documents({}) == 0
    assert post(tutor, "/announcements/new", title="Resource", body="Text", files=(BytesIO(b"Notes"), "notes.txt")).status_code == 303
    first = database.announcements.find_one({})
    file = database.announcement_files.find_one({})
    assert post(tutor, "/announcements/new", title="Second announcement", body="Text").status_code == 303
    second = database.announcements.find_one({"_id": {"$ne": first["_id"]}})
    assert tutor.get(f"/announcements/{second['_id']}/files/{file['_id']}").status_code == 404
    assert post(tutor, f"/announcements/{second['_id']}/edit", title="Edited", body="Text", remove_files=str(file["_id"])).status_code == 404
    assert database.announcement_files.count_documents({}) == 1
