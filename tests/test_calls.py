from datetime import timedelta

from bson import ObjectId
import pytest

from test_portal import app, database, users, csrf, lesson_record, now, sign_in


def path(lesson, suffix=''):
    return f"/lessons/{lesson['_id']}/call{suffix}"


def send(client, lesson, suffix, payload):
    return client.post(path(lesson, suffix), json=payload, headers={'X-CSRFToken': csrf(client)})


def join(client, lesson):
    response = send(client, lesson, '/session', {'action': 'join'})
    assert response.status_code == 201
    return response.json['id']


def test_call_is_lesson_scoped_and_preflight_available(app, database, users):
    lesson = lesson_record(database, users, 'student', now())
    for role in ['tutor', 'parent', 'student']:
        client = sign_in(app, users[role])
        response = client.get(path(lesson))
        assert response.status_code == 302
        assert response.location.endswith(f"/lessons/{lesson['_id']}/whiteboard?call=1")
        board = client.get(response.location)
        assert board.status_code == 200
        assert b'data-call-dialog' in board.data
        assert b'data-call-open' in board.data
        assert b'Record microphone test' in board.data
        assert b'Test / apply devices' not in board.data
        assert b'call.js' in board.data
    for role in ['other_tutor', 'other_parent', 'sibling', 'other_student']:
        client = sign_in(app, users[role])
        assert client.get(path(lesson)).status_code == 404
        assert send(client, lesson, '/session', {'action': 'join'}).status_code == 404
    database.lessons.update_one({'_id': lesson['_id']}, {'$set': {'status': 'cancelled'}})
    assert sign_in(app, users['student']).get(path(lesson)).status_code == 409


def test_call_rendezvous_delivers_only_to_own_lesson_session(app, database, users):
    lesson = lesson_record(database, users, 'student', now())
    other = lesson_record(database, users, 'sibling', now())
    tutor, student, parent = [sign_in(app, users[role]) for role in ['tutor', 'student', 'parent']]
    tutor_id, student_id, parent_id = join(tutor, lesson), join(student, lesson), join(parent, lesson)
    other_id = join(tutor, other)
    payload = {'id': tutor_id, 'target': student_id, 'kind': 'description', 'data': {'type': 'offer', 'sdp': 'v=0\r\n'}}
    assert send(tutor, lesson, '/signals', payload).status_code == 201
    result = student.get(path(lesson, f'/signals?id={student_id}')).json
    assert len(result['signals']) == 1
    assert result['signals'][0]['sender'] == tutor_id
    assert result['signals'][0]['data'] == payload['data']
    assert student.get(path(lesson, f'/signals?id={student_id}')).json == result
    assert send(student, lesson, '/signals/ack', {'id': student_id, 'messages': [result['signals'][0]['id']]}).json == {'acknowledged': True}
    assert student.get(path(lesson, f'/signals?id={student_id}')).json['signals'] == []
    assert parent.get(path(lesson, f'/signals?id={parent_id}')).json['signals'] == []
    assert parent.get(path(lesson, f'/signals?id={student_id}')).status_code == 409
    assert send(parent, lesson, '/signals', payload).status_code == 409
    assert send(tutor, lesson, '/signals', {**payload, 'target': other_id}).status_code == 404
    assert tutor.get(path(other, f'/signals?id={tutor_id}')).status_code == 409
    response = send(parent, lesson, '/session', {'action': 'heartbeat', 'id': parent_id})
    assert {person['id'] for person in response.json['participants']} == {tutor_id, student_id, parent_id}
    assert send(student, lesson, '/session', {'action': 'leave', 'id': student_id}).json == {'left': True}
    assert student.get(path(lesson, f'/signals?id={student_id}')).status_code == 409
    assert send(tutor, lesson, '/signals', payload).status_code == 404
    assert database.call_signals.count_documents({'target_id': ObjectId(student_id)}) == 0


def test_call_signals_validate_csrf_payloads_and_expiry(app, database, users):
    lesson = lesson_record(database, users, 'student', now())
    tutor, student = [sign_in(app, users[role]) for role in ['tutor', 'student']]
    first, second = join(tutor, lesson), join(student, lesson)
    assert tutor.post(path(lesson, '/session'), json={'action': 'join'}).status_code == 400
    payload = {'id': first, 'target': second, 'kind': 'description', 'data': {'type': 'offer', 'sdp': 'v=0'}}
    for change in [{'kind': 'unknown'}, {'data': None}, {'data': {'type': 'rollback', 'sdp': 'v=0'}}, {'data': {'type': 'offer', 'sdp': 'x' * 60001}}, {'kind': 'candidate', 'data': {'candidate': False}}, {'kind': 'candidate', 'data': {'candidate': 'candidate:x', 'sdpMLineIndex': True}}]:
        assert send(tutor, lesson, '/signals', {**payload, **change}).status_code == 400
    assert send(tutor, lesson, '/signals', {**payload, 'kind': 'candidate', 'data': {'candidate': 'candidate:x', 'sdpMid': '0', 'sdpMLineIndex': 0}}).status_code == 201
    database.call_participants.update_one({'_id': ObjectId(first)}, {'$set': {'seen_at': now() - timedelta(minutes=1)}})
    assert send(tutor, lesson, '/session', {'action': 'heartbeat', 'id': first}).status_code == 409
    assert send(tutor, lesson, '/signals', payload).status_code == 409


def test_turn_credentials_are_temporary_and_ice_config_validated(app, database, users):
    from portal.calls import ice_servers
    from flask import g
    with app.test_request_context():
        g.user = users['tutor']
        app.config.update(CALL_ICE_SERVERS='[{"urls":"stun:stun.example.test:3478"}]', CALL_TURN_URLS='turn:relay.example.test:3478,turns:relay.example.test:5349', CALL_TURN_SECRET='test-secret')
        servers = ice_servers()
        assert servers[0]['urls'].startswith('stun:')
        assert servers[1]['username'].endswith(':' + str(users['tutor']['_id']))
        assert now().timestamp() < int(servers[1]['username'].split(':')[0]) < (now() + timedelta(hours=5)).timestamp()
        assert servers[1]['credential'] != 'test-secret'
        from portal.core import Problem
        for invalid in ['{}', '[{"urls":"https://invalid.test"}]', '[1]', 'not-json']:
            app.config['CALL_ICE_SERVERS'] = invalid
            with pytest.raises(Problem):
                ice_servers()


def test_signals_from_different_workers_cannot_be_skipped(app, database, users):
    lesson = lesson_record(database, users, 'student', now())
    tutor, student = [sign_in(app, users[role]) for role in ['tutor', 'student']]
    first, second = join(tutor, lesson), join(student, lesson)
    record = {'lesson_id': lesson['_id'], 'sender_id': ObjectId(first), 'target_id': ObjectId(second),
              'kind': 'candidate', 'data': {'candidate': 'candidate:x'}, 'created_at': now(), 'expires_at': now() + timedelta(minutes=2)}
    newer = ObjectId('ffffffffffffffffffffffff')
    older = ObjectId('000000010000000000000001')
    database.call_signals.insert_one({'_id': newer, **record})
    assert student.get(path(lesson, f'/signals?id={second}')).json['signals'][0]['id'] == str(newer)
    # A delayed insertion has a lower ObjectId than a packet already delivered.
    database.call_signals.insert_one({'_id': older, **record})
    assert send(student, lesson, '/signals/ack', {'id': second, 'messages': [str(newer)]}).status_code == 200
    assert student.get(path(lesson, f'/signals?id={second}&after={newer}')).json['signals'][0]['id'] == str(older)
    assert send(tutor, lesson, '/signals/ack', {'id': first, 'messages': [str(older)]}).status_code == 200
    assert database.call_signals.find_one({'_id': older}) is not None


def test_call_media_states_are_shared_and_validated(app, database, users):
    lesson = lesson_record(database, users, 'student', now())
    tutor, student = [sign_in(app, users[role]) for role in ['tutor', 'student']]
    response = send(tutor, lesson, '/session', {'action': 'join', 'camera_on': False, 'microphone_on': True})
    assert response.status_code == 201
    tutor_id = response.json['id']
    person = next(row for row in response.json['participants'] if row['id'] == tutor_id)
    assert person['camera_on'] is False
    assert person['microphone_on'] is True
    student_id = join(student, lesson)
    updated = send(tutor, lesson, '/session', {'action': 'heartbeat', 'id': tutor_id, 'camera_on': True, 'microphone_on': False})
    assert updated.status_code == 200
    roster = send(student, lesson, '/session', {'action': 'heartbeat', 'id': student_id}).json['participants']
    person = next(row for row in roster if row['id'] == tutor_id)
    assert person['camera_on'] is True
    assert person['microphone_on'] is False
    for invalid in [0, 1, 'false', None, [], {}]:
        assert send(tutor, lesson, '/session', {'action': 'heartbeat', 'id': tutor_id, 'camera_on': invalid}).status_code == 400
    assert send(student, lesson, '/session', {'action': 'heartbeat', 'id': tutor_id, 'microphone_on': True}).status_code == 409
    assert database.call_participants.find_one({'_id': ObjectId(tutor_id)})['microphone_on'] is False


def test_merged_call_obeys_whiteboard_open_time(app, database, users):
    lesson = lesson_record(database, users, 'student', now() + timedelta(hours=1))
    for role in ['parent', 'student']:
        response = sign_in(app, users[role]).get(path(lesson), follow_redirects=True)
        assert response.status_code == 403
        assert b'The whiteboard opens when the lesson starts.' in response.data
