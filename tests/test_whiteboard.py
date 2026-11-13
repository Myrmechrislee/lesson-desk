from datetime import timedelta
from io import BytesIO

from PIL import Image
import pytest
from reportlab.pdfgen import canvas

from test_portal import app, database, users, csrf, lesson_record, now, sign_in


def endpoint(lesson, suffix="state"):
    return f"/lessons/{lesson['_id']}/whiteboard/{suffix}"


def json_post(client, path, data):
    return client.post(path, json=data, headers={"X-CSRFToken": csrf(client)})


def text_object(key="working", **extra):
    return {"id": key, "type": "text", "x": 10, "y": 20, "w": 300, "h": 80,
            "text": "x² + y² = 1", "font": 24, "color": "#243d35", "z": 1, **extra}


def test_shared_whiteboard_conflict_preserves_both_writers(app, database, users):
    lesson = lesson_record(database, users, "student", now() - timedelta(minutes=1))
    tutor, student = (sign_in(app, users[role]) for role in ("tutor", "student"))
    assert tutor.get(endpoint(lesson)).json == {"revision": 0, "elements": []}
    assert student.get(endpoint(lesson)).json["revision"] == 0
    first = text_object()
    assert json_post(tutor, endpoint(lesson), {"revision": 0, "changes": [{"id": first["id"], "element": first}]}).json == {"revision": 1}
    second = text_object("student-working", x=400)
    conflict = json_post(student, endpoint(lesson), {"revision": 0, "changes": [{"id": second["id"], "element": second}]})
    assert conflict.status_code == 409
    assert conflict.json["elements"][0]["text"] == first["text"]
    assert json_post(student, endpoint(lesson), {"revision": 1, "changes": [{"id": second["id"], "element": second}]}).status_code == 200
    shared = tutor.get(endpoint(lesson)).json
    assert {e["id"] for e in shared["elements"]} == {first["id"], second["id"]}
    assert tutor.get(endpoint(lesson) + "?since=2").json == {"revision": 2, "unchanged": True}
    assert json_post(tutor, endpoint(lesson), {"revision": 2, "changes": [{"id": first["id"], "element": None}]}).status_code == 200
    assert [e["id"] for e in student.get(endpoint(lesson)).json["elements"]] == [second["id"]]


@pytest.mark.parametrize("role,allowed", [("tutor", True), ("student", True), ("parent", True), ("sibling", False), ("other_tutor", False), ("other_parent", False)])
def test_whiteboard_access_is_lesson_scoped(app, database, users, role, allowed):
    lesson = lesson_record(database, users, "student", now() - timedelta(minutes=1))
    client = sign_in(app, users[role])
    assert client.get(endpoint(lesson)).status_code == (200 if allowed else 404)
    page = client.get(f"/lessons/{lesson['_id']}/whiteboard")
    assert page.status_code == (200 if allowed else 404)
    if allowed:
        assert b"whiteboard.js" in page.data
        assert b"Circular functions" in page.data


def test_multipage_pdf_upload_and_cross_lesson_asset_protection(app, database, users):
    lesson = lesson_record(database, users, "student", now() - timedelta(minutes=1))
    other = lesson_record(database, users, "sibling", now() + timedelta(days=2))
    client = sign_in(app, users["tutor"])
    pdf = BytesIO()
    doc = canvas.Canvas(pdf)
    for page in range(3):
        doc.drawString(70, 700, f"Worksheet page {page + 1}")
        doc.showPage()
    doc.save()
    result = client.post(endpoint(lesson, "assets"), data={"csrf_token": csrf(client), "file": (BytesIO(pdf.getvalue()), "worksheet.pdf")})
    assert result.status_code == 201
    assets = result.json["assets"]
    assert [asset["page"] for asset in assets] == [1, 2, 3]
    for asset in assets:
        response = client.get(endpoint(lesson, "assets/" + asset["id"]))
        assert response.mimetype == "image/jpeg"
        with Image.open(BytesIO(response.data)) as image:
            assert image.width > 500
            assert max(image.size) <= 1600
    imported = {"id": "worksheet", "type": "image", "x": 0, "y": 0, "w": 600, "h": 800, "asset_id": assets[0]["id"]}
    assert json_post(client, endpoint(lesson), {"revision": 0, "changes": [{"id": "worksheet", "element": imported}]}).status_code == 200
    imported["id"] = "stolen"
    assert json_post(client, endpoint(other), {"revision": 0, "changes": [{"id": "stolen", "element": imported}]}).status_code == 400
    assert client.get(endpoint(other, "assets/" + assets[0]["id"])).status_code == 404
    assert sign_in(app, users["other_parent"]).get(endpoint(lesson, "assets/" + assets[0]["id"])).status_code == 404


def test_whiteboard_validates_mutations_and_pdf_input(app, database, users):
    lesson = lesson_record(database, users, "student", now() - timedelta(minutes=1))
    client = sign_in(app, users["tutor"])
    assert client.post(endpoint(lesson), json={"revision": 0, "changes": []}).status_code == 400
    for bad in [text_object(x=float("inf")), text_object(x=10 ** 400), text_object(type=[]),
                text_object(type="template", template=[]), text_object(color="url(javascript:alert(1))"), text_object(text="x" * 8001)]:
        assert json_post(client, endpoint(lesson), {"revision": 0, "changes": [{"id": "bad", "element": bad}]}).status_code == 400
    assert json_post(client, endpoint(lesson), {"revision": 0, "changes": [{"id": "$bad.dot", "element": text_object()}]}).status_code == 400
    assert client.post(endpoint(lesson, "assets"), data={"csrf_token": csrf(client), "file": (BytesIO(b"%PDF-broken"), "invalid.pdf")}).status_code == 400
    assert database.whiteboard_assets.count_documents({"lesson_id": lesson["_id"]}) == 0


def live_payload(sequence=1, **extra):
    return {"client_id": "tutor-tab", "sequence": sequence, **extra}


def test_live_previews_share_unfinished_work_and_viewers_without_saving(app, database, users):
    lesson = lesson_record(database, users, "student", now() - timedelta(minutes=1))
    tutor, student = (sign_in(app, users[role]) for role in ("tutor", "student"))
    path = text_object("stroke", type="path", points=[[0, 0], [12, 8]])
    response = json_post(tutor, endpoint(lesson, "live"), live_payload(elements=[path]))
    assert response.status_code == 200
    assert response.json["revision"] == 0
    assert response.json["viewers"] == [{"id": str(users["tutor"]["_id"]), "name": "Tutor", "role": "tutor"}]
    assert response.json["previews"] == []  # Own browser uses its immediate local draft.
    json_post(student, endpoint(lesson, "live"), {"client_id": "student-tab", "sequence": 1})
    shared = student.get(endpoint(lesson, "live") + "?client_id=student-tab").json
    assert {viewer["name"] for viewer in shared["viewers"]} == {"Tutor", "Student"}
    assert shared["previews"][0]["elements"][0]["points"] == path["points"]
    assert tutor.get(endpoint(lesson)).json == {"revision": 0, "elements": []}

    # A second packet extends the same stroke before it is finished.
    path["points"].append([30, 12])
    json_post(tutor, endpoint(lesson, "live"), live_payload(2, elements=[path]))
    assert student.get(endpoint(lesson, "live")).json["previews"][0]["elements"][0]["points"][-1] == [30, 12]
    text = text_object(text="Typing a proof…")
    json_post(tutor, endpoint(lesson, "live"), live_payload(3, elements=[text]))
    assert student.get(endpoint(lesson, "live")).json["previews"][0]["elements"][0]["text"] == "Typing a proof…"

    # Clearing a preview and then receiving a delayed packet must keep it clear.
    json_post(tutor, endpoint(lesson, "live"), live_payload(5, elements=[]))
    json_post(tutor, endpoint(lesson, "live"), live_payload(4, elements=[path]))
    assert student.get(endpoint(lesson, "live")).json["previews"] == []
    json_post(tutor, endpoint(lesson, "live"), live_payload(6, leave=True))
    assert [viewer["name"] for viewer in student.get(endpoint(lesson, "live")).json["viewers"]] == ["Student"]
    assert json_post(tutor, endpoint(lesson), {"revision": 0, "changes": [{"id": text["id"], "element": text}]}).status_code == 200
    assert student.get(endpoint(lesson, "live")).json["revision"] == 1


def test_live_preview_expiry_heartbeat_and_multiple_tabs(app, database, users, monkeypatch):
    import portal.whiteboard as whiteboard

    lesson = lesson_record(database, users, "student", now() - timedelta(minutes=1))
    tutor, student = (sign_in(app, users[role]) for role in ("tutor", "student"))
    timestamp = now()
    monkeypatch.setattr(whiteboard, "now", lambda: timestamp)
    json_post(tutor, endpoint(lesson, "live"), live_payload(elements=[text_object()]))
    json_post(tutor, endpoint(lesson, "live"), {"client_id": "second-tab", "sequence": 1})
    assert len(student.get(endpoint(lesson, "live")).json["viewers"]) == 1
    timestamp += timedelta(seconds=9)
    shared = student.get(endpoint(lesson, "live")).json
    assert shared["previews"] == []
    assert len(shared["viewers"]) == 1
    json_post(tutor, endpoint(lesson, "live"), live_payload(2))
    assert student.get(endpoint(lesson, "live")).json["previews"] == []  # Heartbeat cannot revive expired ink.
    timestamp += timedelta(seconds=31)
    assert student.get(endpoint(lesson, "live")).json == {"revision": 0, "viewers": [], "previews": []}


def test_live_revision_is_read_after_preview_changes(app, database, users, monkeypatch):
    from pymongo.synchronous.collection import Collection

    lesson = lesson_record(database, users, "student", now() - timedelta(minutes=1))
    client = sign_in(app, users["student"])
    client.get(endpoint(lesson))
    original_find = Collection.find

    def find(collection, *args, **kwargs):
        cursor = original_find(collection, *args, **kwargs)
        if collection.name != "whiteboard_live":
            return cursor

        def concurrent_save():
            yield from cursor
            # A collaborator committed and cleared their draft as this poll
            # completed. The matching board revision must be returned too.
            database.whiteboards.update_one({"_id": lesson["_id"]}, {"$set": {"revision": 1}})

        return concurrent_save()

    monkeypatch.setattr(Collection, "find", find)
    response = client.get(endpoint(lesson, "live"))
    assert response.json == {"revision": 1, "viewers": [], "previews": []}


def test_live_sessions_are_lesson_scoped_and_validate_input(app, database, users):
    lesson = lesson_record(database, users, "student", now() - timedelta(minutes=1))
    other = lesson_record(database, users, "sibling", now() + timedelta(days=2))
    tutor = sign_in(app, users["tutor"])
    json_post(tutor, endpoint(lesson, "live"), live_payload(elements=[text_object()]))
    assert tutor.get(endpoint(other, "live")).json == {"revision": 0, "viewers": [], "previews": []}
    outsider = sign_in(app, users["other_tutor"])
    assert outsider.get(endpoint(lesson, "live")).status_code == 404
    assert json_post(outsider, endpoint(lesson, "live"), live_payload()).status_code == 404
    assert tutor.post(endpoint(lesson, "live"), json=live_payload()).status_code == 400
    for payload in [live_payload(client_id="$invalid"), live_payload(sequence=True), live_payload(sequence=-1),
                    live_payload(elements={}), live_payload(elements=[text_object(color="red")]),
                    live_payload(elements=[text_object(), text_object()]), live_payload(leave="yes")]:
        assert json_post(tutor, endpoint(lesson, "live"), payload).status_code == 400


def test_shape_styles_and_blank_templates_round_trip(app, database, users):
    lesson = lesson_record(database, users, "student", now() - timedelta(minutes=1))
    client = sign_in(app, users["tutor"])
    changes = [
        {"id": "shape", "element": text_object("shape", type="polygon", sides=5, fill="#f3bc74", width=8)},
        {"id": "blank", "element": text_object("blank", type="template", template="cartesian-blank")},
        {"id": "sets", "element": text_object("sets", type="template", template="venn")},
        {"id": "outline", "element": text_object("outline", type="ellipse", fill="transparent")},
    ]
    assert json_post(client, endpoint(lesson), {"revision": 0, "changes": changes}).status_code == 200
    elements = {item["id"]: item for item in client.get(endpoint(lesson)).json["elements"]}
    assert elements["shape"]["sides"] == 5
    assert elements["shape"]["fill"] == "#f3bc74"
    assert elements["shape"]["width"] == 8
    assert elements["blank"]["template"] == "cartesian-blank"
    assert elements["sets"]["template"] == "venn"
    assert elements["outline"]["fill"] == "transparent"
    for extra in [{"sides": 2}, {"sides": 13}, {"sides": True}, {"sides": 3.5},
                  {"sides": 6, "fill": "url(javascript:bad)"}, {"sides": 6, "fill": []}]:
        element = text_object("bad", type="polygon", **extra)
        assert json_post(client, endpoint(lesson), {"revision": 1, "changes": [{"id": "bad", "element": element}]}).status_code == 400


@pytest.mark.parametrize("role", ["parent", "student"])
def test_whiteboard_closed_before_lesson_on_all_routes(app, database, users, role):
    lesson = lesson_record(database, users, "student", now() + timedelta(hours=1))
    client = sign_in(app, users[role])
    base = f"/lessons/{lesson['_id']}/whiteboard"
    for suffix in ["", "/state", "/live", "/assets/000000000000000000000001"]:
        response = client.get(base + suffix)
        assert response.status_code == 403
        assert b"opens when the lesson starts" in response.data
    for suffix, payload in [("/state", {"revision": 0, "changes": []}), ("/live", live_payload())]:
        assert json_post(client, base + suffix, payload).status_code == 403
    assert client.post(base + "/assets", data={"csrf_token": csrf(client)}).status_code == 403
    assert database.whiteboards.count_documents({"_id": lesson['_id']}) == 0
    tutor = sign_in(app, users['tutor'])
    assert tutor.get(base).status_code == 200
    assert b'data-board-tool="pan"' not in tutor.get(base).data
    assert b'<small>Select</small>' in tutor.get(base).data
