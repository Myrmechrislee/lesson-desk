from io import BytesIO
from zipfile import ZipFile

from PIL import Image

from test_portal import app, database, users, sign_in, post


def pdf(name="work.pdf"):
    return BytesIO(b"%PDF-1.4\n1 0 obj <<>> endobj\n%%EOF"), name


def assign(tutor, student_id, **overrides):
    return post(tutor, "/homework/new", student_id=str(student_id), title="Homework review",
                instructions="Complete the attached questions", **overrides)


def test_family_homework_documents_completion_and_marking(app, database, users):
    tutor = sign_in(app, users["tutor"])
    parent = sign_in(app, users["parent"])
    student = sign_in(app, users["student"])
    assert assign(tutor, users["student"]["_id"], files=pdf("questions.pdf")).status_code == 303
    assignment = database.homework.find_one({})
    path = f"/homework/{assignment['_id']}"
    resource = database.homework_photos.find_one({"kind": "assignment"})
    resource_url = f"{path}/files/{resource['_id']}"
    assert parent.get("/homework").status_code == 200
    assert b"In progress" in parent.get("/homework").data
    assert b"Assignment files" in parent.get(path).data
    assert student.get(resource_url).mimetype == "application/pdf"
    assert "attachment" in student.get(resource_url).headers["Content-Disposition"]
    assert post(student, resource_url + "/delete").status_code == 404
    assert post(tutor, path + "/marked", files=pdf("marked.pdf")).status_code == 409
    assert not database.homework_photos.find_one({"kind": "marked"})
    assert post(student, path + "/submit").status_code == 400

    assert post(parent, path + "/files", files=pdf("completed.pdf")).status_code == 303
    submitted_file = database.homework_photos.find_one({"kind": "submission"})
    assert submitted_file["uploaded_by"] == users["parent"]["_id"]
    assert b"completed.pdf" in student.get(path).data
    assert post(parent, path + "/submit", submission_notes="I showed my working.").status_code == 303
    assert b"Submitted for marking" in parent.get("/homework").data
    assert b"I showed my working." in tutor.get(path).data
    assert post(tutor, path + "/marked", files=pdf("marked.pdf")).status_code == 303
    current = database.homework.find_one({"_id": assignment["_id"]})
    assert current["marked_at"] and current["marking_feedback"] == ""
    assert b"Marked homework" in parent.get(path).data
    assert b"Marked" in student.get("/homework").data

    assert post(tutor, path + "/marked", files=pdf("more-marking.pdf"),
                marking_feedback="Check question three.").status_code == 303
    assert b"Check question three." in parent.get(path).data
    assert b"Check question three." in student.get(path).data
    file_url = f"{path}/files/{submitted_file['_id']}"
    assert post(student, file_url + "/delete").status_code == 303
    assert student.get(file_url).status_code == 404
    assert "submitted_at" not in database.homework.find_one({"_id": assignment["_id"]})
    assert b"earlier submission" in student.get(path).data
    assert post(tutor, path + "/marked", files=pdf("must-wait.pdf")).status_code == 409


def test_homework_access_tracks_current_parent_and_isolates_files(app, database, users):
    tutor = sign_in(app, users["tutor"])
    parent = sign_in(app, users["parent"])
    stranger = sign_in(app, users["other_parent"])
    assert assign(tutor, users["student"]["_id"]).status_code == 303
    first = database.homework.find_one({"student_id": users["student"]["_id"]})
    assert assign(tutor, users["sibling"]["_id"]).status_code == 303
    sibling = database.homework.find_one({"student_id": users["sibling"]["_id"]})
    assert assign(tutor, users["other_student"]["_id"]).status_code == 303
    foreign = database.homework.find_one({"student_id": users["other_student"]["_id"]})
    path = f"/homework/{first['_id']}"
    assert parent.get(f"/homework/{sibling['_id']}").status_code == 200
    assert parent.get(f"/homework/{foreign['_id']}").status_code == 404
    assert stranger.get(path).status_code == 404
    assert post(stranger, path + "/files", files=pdf()).status_code == 404
    assert post(sign_in(app, users["sibling"]), path + "/submit", submission_notes="No access").status_code == 404
    assert sign_in(app, users["other_tutor"]).get(path).status_code == 404

    assert post(parent, path + "/files", files=pdf()).status_code == 303
    item = database.homework_photos.find_one({"homework_id": first["_id"]})
    assert parent.get(f"/homework/{sibling['_id']}/files/{item['_id']}").status_code == 404
    assert post(tutor, f"{path}/files/{item['_id']}/delete").status_code == 403
    database.users.update_one({"_id": users["student"]["_id"]}, {"$set": {"parent_id": users["other_parent"]["_id"]}})
    assert parent.get(path).status_code == 404
    assert parent.get(f"{path}/files/{item['_id']}").status_code == 404
    assert post(parent, f"{path}/files/{item['_id']}/delete").status_code == 404
    assert stranger.get(path).status_code == 200
    assert post(stranger, f"{path}/files/{item['_id']}/delete").status_code == 303


def test_homework_validates_all_uploads_before_saving_and_supports_word(app, database, users):
    tutor = sign_in(app, users["tutor"])
    student = sign_in(app, users["student"])
    assert assign(tutor, users["student"]["_id"], files=(BytesIO(b"invalid"), "worksheet.pdf")).status_code == 400
    assert database.homework.count_documents({}) == 0
    assert assign(tutor, users["student"]["_id"]).status_code == 303
    assignment = database.homework.find_one({})
    path = f"/homework/{assignment['_id']}"
    assert post(student, path + "/files", files=[pdf(), (BytesIO(b"<script>bad</script>"), "bad.pdf")]).status_code == 400
    assert database.homework_photos.count_documents({}) == 0
    assert post(student, path + "/files", files=[pdf(f"work{i}.pdf") for i in range(6)]).status_code == 400
    assert post(student, path + "/files", files=(BytesIO(b"x" * (8 * 1024 * 1024 + 1)), "large.txt")).status_code == 413
    assert database.homework_photos.count_documents({}) == 0

    word = BytesIO()
    with ZipFile(word, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    word.seek(0)
    assert post(student, path + "/files", files=(word, "answers.docx")).status_code == 303
    assert post(student, path + "/files", files=(BytesIO(b"My work"), "answers.txt")).status_code == 303
    assert post(student, path + "/files", files=pdf("作业.pdf")).status_code == 303
    assert database.homework_photos.find_one({"filename": "homework-file.pdf"})
    image = BytesIO()
    Image.new("RGB", (10, 10), "blue").save(image, format="PNG")
    image.seek(0)
    assert post(student, path + "/photos", photos=(image, "answer.png")).status_code == 303
    item = database.homework_photos.find_one({"mimetype": "image/jpeg"})
    assert student.get(f"{path}/photos/{item['_id']}").data.startswith(b"\xff\xd8")
    assert database.homework_photos.count_documents({"kind": "submission"}) == 4
    assert post(student, path + "/submit", submission_notes="All done.").status_code == 303
    assert b"All done." in sign_in(app, users["parent"]).get(path).data
    assert post(student, path + "/submit", action="save", submission_notes="Revised notes.").status_code == 303
    assert "submitted_at" not in database.homework.find_one({"_id": assignment["_id"]})


def test_tutor_live_work_updates_include_documents_and_removals(app, database, users):
    tutor = sign_in(app, users["tutor"])
    parent = sign_in(app, users["parent"])
    assert assign(tutor, users["student"]["_id"]).status_code == 303
    assignment = database.homework.find_one({})
    path = f"/homework/{assignment['_id']}"
    response = tutor.get(path + "/live", buffered=False)
    try:
        chunks = iter(response.response)
        assert b"retry:" in next(chunks)
        assert b"data: []" in next(chunks)
        assert post(parent, path + "/files", files=pdf("completed.pdf")).status_code == 303
        update = next(chunks)
        assert b"completed.pdf" in update and b'"is_image": false' in update
        item = database.homework_photos.find_one({"kind": "submission"})
        assert post(parent, f"{path}/files/{item['_id']}/delete").status_code == 303
        assert b"data: []" in next(chunks)
    finally:
        response.close()


def test_tutor_grades_submitted_work_without_attachments(app, database, users):
    tutor = sign_in(app, users["tutor"])
    student = sign_in(app, users["student"])
    assert assign(tutor, users["student"]["_id"]).status_code == 303
    assignment = database.homework.find_one({})
    path = f"/homework/{assignment['_id']}"
    assert post(student, path + "/submit", submission_notes="Finished on paper.").status_code == 303
    assert post(tutor, path + "/marked", grade="9/10", marking_feedback="Well done.").status_code == 303
    assert database.homework_photos.count_documents({}) == 0
    for role in ("parent", "student", "tutor"):
        page = sign_in(app, users[role]).get(path)
        assert b"9/10" in page.data and b"Well done." in page.data
        assert b"Marked homework" in page.data
    assert b"Marked" in student.get("/homework").data
    assert post(tutor, path + "/marked").status_code == 303


def test_tutor_offline_completion_requires_explicit_override_and_can_be_reopened(app, database, users):
    tutor = sign_in(app, users["tutor"])
    parent = sign_in(app, users["parent"])
    assert assign(tutor, users["student"]["_id"]).status_code == 303
    assignment = database.homework.find_one({})
    path = f"/homework/{assignment['_id']}"
    assert post(tutor, path + "/marked", grade="A").status_code == 409
    assert post(parent, path + "/marked", grade="A", complete_without_submission="yes").status_code == 403
    assert post(sign_in(app, users["other_tutor"]), path + "/marked", grade="A", complete_without_submission="yes").status_code == 404
    assert post(tutor, path + "/marked", grade="x" * 101, complete_without_submission="yes").status_code == 400
    assert post(tutor, path + "/marked", grade="A", complete_without_submission="yes").status_code == 303
    current = database.homework.find_one({"_id": assignment["_id"]})
    assert current["submission_source"] == "tutor_override"
    assert current["marked_by"] == current["submitted_by"] == users["tutor"]["_id"]
    assert current["marked_submission_at"] == current["submitted_at"]
    assert database.homework_photos.count_documents({}) == 0
    assert b"completed offline by the tutor" in parent.get(path).data
    assert b"Marked" in parent.get("/homework").data
    assert post(parent, path + "/files", files=pdf()).status_code == 303
    assert b"earlier submission" in parent.get(path).data
    current = database.homework.find_one({"_id": assignment["_id"]})
    assert "submitted_at" not in current and "submission_source" not in current
    assert b"In progress" in parent.get("/homework").data
    assert post(parent, path + "/submit", submission_notes="New working.").status_code == 303
    assert database.homework.find_one({"_id": assignment["_id"]})["submission_source"] == "family"
