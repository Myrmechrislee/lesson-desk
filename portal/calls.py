"""Lesson-scoped WebRTC rendezvous; media stays encrypted between browsers."""
from datetime import timedelta
import base64
import hashlib
import hmac
import json

from bson import ObjectId
from flask import Blueprint, abort, current_app, g, redirect, request, url_for

from .core import Problem, db, now, object_id, requires
from .lessons import get_lesson

bp = Blueprint('calls', __name__)
PRESENCE_SECONDS = 35


def call_lesson(lesson_id):
    lesson = get_lesson(lesson_id)
    if lesson['status'] in {'cancelled', 'cancelled_late'}:
        raise Problem('This lesson has been cancelled.', 409)
    return lesson


def ice_servers():
    configured = current_app.config.get('CALL_ICE_SERVERS', '[]')
    try:
        servers = json.loads(configured) if isinstance(configured, str) else configured
        if not isinstance(servers, list):
            raise ValueError
        for server in servers:
            urls = server['urls']
            if isinstance(urls, str):
                urls = [urls]
            if not isinstance(urls, list) or not urls or any(not isinstance(url, str) or not url.startswith(('stun:', 'stuns:', 'turn:', 'turns:')) for url in urls):
                raise ValueError
    except (ValueError, TypeError, KeyError):
        raise Problem('Video calling is not configured correctly. Ask the tutor to contact support.', 503) from None
    secret = current_app.config.get('CALL_TURN_SECRET', '')
    urls = current_app.config.get('CALL_TURN_URLS', '')
    if secret and urls:
        username = f"{int((now() + timedelta(hours=4)).timestamp())}:{g.user['_id']}"
        credential = base64.b64encode(hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()).decode()
        servers = [*servers, {'urls': [url.strip() for url in urls.split(',') if url.strip()], 'username': username, 'credential': credential}]
    return servers


@bp.get('/lessons/<lesson_id>/call')
@requires('tutor', 'parent', 'student')
def room(lesson_id):
    call_lesson(lesson_id)
    return redirect(url_for('whiteboard.board', lesson_id=lesson_id, call=1))


def participant(lesson_id, value):
    row = db().call_participants.find_one({
        '_id': object_id(value), 'lesson_id': lesson_id, 'user_id': g.user['_id'],
        'seen_at': {'$gt': now() - timedelta(seconds=PRESENCE_SECONDS)},
    })
    if not row:
        raise Problem('Rejoin the call to reconnect.', 409)
    return row


def roster(lesson_id):
    return [{'id': str(row['_id']), 'name': row['name'], 'role': row['role'],
             'camera_on': row.get('camera_on', True), 'microphone_on': row.get('microphone_on', True)}
            for row in db().call_participants.find({'lesson_id': lesson_id, 'seen_at': {'$gt': now() - timedelta(seconds=PRESENCE_SECONDS)}}).sort('_id', 1)]


@bp.post('/lessons/<lesson_id>/call/session')
@requires('tutor', 'parent', 'student')
def session(lesson_id):
    lesson = call_lesson(lesson_id)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise Problem('Reload the call before joining.')
    action = payload.get('action')
    timestamp = now()
    media = {}
    for key in ('camera_on', 'microphone_on'):
        if key in payload:
            if type(payload[key]) is not bool:
                raise Problem('Choose whether your camera and microphone are on.')
            media[key] = payload[key]
    if action == 'join':
        # Limit abandoned sessions per account without rejecting a quick reconnect.
        db().call_participants.delete_many({'lesson_id': lesson['_id'], 'user_id': g.user['_id'], 'seen_at': {'$lt': timestamp - timedelta(seconds=PRESENCE_SECONDS)}})
        if db().call_participants.count_documents({'lesson_id': lesson['_id'], 'user_id': g.user['_id']}) >= 3:
            raise Problem('Close another call tab before joining again.', 409)
        row = {'_id': ObjectId(), 'lesson_id': lesson['_id'], 'user_id': g.user['_id'],
               'name': g.user['name'], 'role': g.user['role'], 'seen_at': timestamp,
               'expires_at': timestamp + timedelta(seconds=PRESENCE_SECONDS), **media}
        db().call_participants.insert_one(row)
        return {'id': str(row['_id']), 'participants': roster(lesson['_id'])}, 201
    row = participant(lesson['_id'], payload.get('id'))
    if action == 'leave':
        db().call_participants.delete_one({'_id': row['_id']})
        db().call_signals.delete_many({'lesson_id': lesson['_id'], '$or': [{'sender_id': row['_id']}, {'target_id': row['_id']}]})
        return {'left': True}
    if action != 'heartbeat':
        raise Problem('Choose a supported call action.')
    db().call_participants.update_one({'_id': row['_id']}, {'$set': {'seen_at': timestamp, 'expires_at': timestamp + timedelta(seconds=PRESENCE_SECONDS), **media}})
    return {'participants': roster(lesson['_id'])}


@bp.get('/lessons/<lesson_id>/call/signals')
@requires('tutor', 'parent', 'student')
def signals(lesson_id):
    lesson = call_lesson(lesson_id)
    row = participant(lesson['_id'], request.args.get('id'))
    # ObjectIds are not globally ordered across gunicorn workers. Deliver until
    # explicitly acknowledged; a delayed insert can never be skipped by a cursor.
    messages = list(db().call_signals.find({'lesson_id': lesson['_id'], 'target_id': row['_id'], 'expires_at': {'$gt': now()}}).sort('_id', 1).limit(100))
    return {'signals': [{'id': str(message['_id']), 'sender': str(message['sender_id']), 'kind': message['kind'], 'data': message['data']} for message in messages]}


@bp.post('/lessons/<lesson_id>/call/signals/ack')
@requires('tutor', 'parent', 'student')
def acknowledge(lesson_id):
    lesson = call_lesson(lesson_id)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get('messages'), list) or not 1 <= len(payload['messages']) <= 100:
        raise Problem('Acknowledge up to 100 call messages.')
    row = participant(lesson['_id'], payload.get('id'))
    ids = [object_id(value) for value in payload['messages']]
    db().call_signals.delete_many({'lesson_id': lesson['_id'], 'target_id': row['_id'], '_id': {'$in': ids}})
    return {'acknowledged': True}


@bp.post('/lessons/<lesson_id>/call/signals')
@requires('tutor', 'parent', 'student')
def send_signal(lesson_id):
    lesson = call_lesson(lesson_id)
    if request.content_length and request.content_length > 70000:
        raise Problem('Call signaling message is too large.', 413)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise Problem('Reload the call to reconnect.')
    row = participant(lesson['_id'], payload.get('id'))
    target = object_id(payload.get('target'))
    if target == row['_id'] or not db().call_participants.find_one({'_id': target, 'lesson_id': lesson['_id'], 'seen_at': {'$gt': now() - timedelta(seconds=PRESENCE_SECONDS)}}):
        abort(404)
    kind, data = payload.get('kind'), payload.get('data')
    if kind not in {'description', 'candidate'} or not isinstance(data, dict):
        raise Problem('Invalid call signaling message.')
    if kind == 'description':
        if data.get('type') not in {'offer', 'answer'} or not isinstance(data.get('sdp'), str) or len(data['sdp']) > 60000:
            raise Problem('Invalid call description.')
        data = {'type': data['type'], 'sdp': data['sdp']}
    else:
        if (not isinstance(data.get('candidate'), str) or len(data['candidate']) > 8000
                or not (data.get('sdpMid') is None or isinstance(data['sdpMid'], str) and len(data['sdpMid']) < 256)
                or not (data.get('sdpMLineIndex') is None or type(data['sdpMLineIndex']) is int and 0 <= data['sdpMLineIndex'] <= 100)):
            raise Problem('Invalid network candidate.')
        data = {key: data.get(key) for key in ('candidate', 'sdpMid', 'sdpMLineIndex')}
    timestamp = now()
    if db().call_signals.count_documents({'sender_id': row['_id'], 'created_at': {'$gt': timestamp - timedelta(seconds=10)}}) >= 200:
        raise Problem('Call is reconnecting. Try again shortly.', 429)
    db().call_signals.insert_one({'lesson_id': lesson['_id'], 'sender_id': row['_id'], 'target_id': target,
                                 'kind': kind, 'data': data, 'created_at': timestamp,
                                 'expires_at': timestamp + timedelta(minutes=2)})
    return {'sent': True}, 201
