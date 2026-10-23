"""Exercise merged T22 routes against local synthetic accounts without printing data."""
import csv
import hashlib
import io
import json
import os
import uuid
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

base = os.environ.get('T22_SMOKE_BASE_URL', 'http://127.0.0.1:18730').rstrip('/')
accounts = {row['role']: row for row in json.loads(
    Path(os.environ['T22_DEMO_ACCOUNTS']).read_text(encoding='utf-8-sig'))
    if row['username'].startswith('a_')}
out = []


def actor(role):
    jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    with opener.open(base + '/app', timeout=10) as response:
        assert response.status == 200

    def send(path, payload=None, idempotency_key=None, accept=None):
        headers = {'Accept': accept or ('application/json' if payload is None else 'text/csv')}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'
            headers['X-CSRFToken'] = next(cookie.value for cookie in jar if cookie.name == 'csrftoken')
            if idempotency_key:
                headers['Idempotency-Key'] = idempotency_key
        request = Request(base + path, data=data, headers=headers)
        try:
            with opener.open(request, timeout=20) as response:
                return response.status, response.headers, response.read()
        except HTTPError as error:
            return error.code, error.headers, error.read()

    login = accounts[role]
    status, _, raw = send('/api/login', {'username': login['username'], 'password': login['password']})
    assert status == 200 and json.loads(raw)['role'] == role, (role, status)
    return send


def csv_check(role, path, payload):
    send = actor(role)
    status, headers, data = send(path, payload)
    assert status == 200, (role, path, status)
    length = len(data)
    assert length > 0
    assert int(headers['Content-Length']) == int(headers['X-Byte-Count']) == length
    assert hashlib.sha256(data).hexdigest() == headers['X-Content-SHA256']
    rows = list(csv.reader(io.StringIO(data.decode('utf-8'))))
    assert len(rows) >= 1
    out.append({'role': role, 'path': path, 'status': status, 'rows': len(rows) - 1,
                'bytes': length, 'sha256_match': True})


manager = actor('manager')
status, _, data = manager('/api/management/work-divisions')
assert status == 200 and isinstance(json.loads(data)['items'], list)
out.append({'role': 'manager', 'path': '/api/management/work-divisions', 'status': status})
if os.environ.get('T22_SMOKE_MUTATE_WORK_DIVISION') == '1':
    existing = {(item['work_item_ref'], item['stage_ref']) for item in json.loads(data)['items']}
    status, _, raw = manager('/api/candidates')
    assert status == 200
    choice = None
    for candidate in json.loads(raw)['items']:
        ref = candidate['candidate_ref']
        if (ref, 'rule_review') in existing:
            continue
        query = urlencode({'work_item_ref': ref, 'stage_ref': 'rule_review'})
        status, _, raw = manager('/api/management/work-divisions/evidence?' + query)
        assert status == 200
        evidence = json.loads(raw)['source_event_refs']
        if evidence:
            choice = (ref, evidence[0])
            break
    assert choice is not None, 'No unregistered synthetic candidate with review evidence'
    ref, evidence_ref = choice
    payload = {'work_item_ref': ref, 'stage_ref': 'rule_review',
               'plan_version_ref': None, 'actual_version_ref': None,
               'plan_dimensions': {'content_generation': None, 'data_entry': 'manager', 'human_decision': 'manager'},
               'source_event_refs': [evidence_ref], 'manual_actual_evidence': [],
               'reason': '共享合成环境登记联调'}
    status, _, raw = manager('/api/management/work-divisions', payload,
                             idempotency_key=str(uuid.uuid4()), accept='application/json')
    assert status == 200, ('register', status)
    item = json.loads(raw)
    assert item['comparison']['state'] == 'consistent' and len(item['maintenance_history']) == 1
    review = {k: item[k] for k in ['work_item_ref', 'stage_ref', 'plan_version_ref', 'actual_version_ref']}
    review.update({'redacted_evidence': item['source_event_refs'], 'conclusion': 'confirmed_consistent',
                   'reason': '共享合成环境人工复核联调'})
    status, _, raw = manager('/api/management/work-divisions/reviews', review,
                             idempotency_key=str(uuid.uuid4()), accept='application/json')
    assert status == 200, ('review', status)
    item = json.loads(raw)
    assert item['review_history'][-1]['conclusion'] == 'confirmed_consistent'
    update = {**payload, 'plan_version_ref': item['plan_version_ref'],
              'actual_version_ref': item['actual_version_ref'], 'reason': '共享合成环境版本留痕联调'}
    status, _, raw = manager('/api/management/work-divisions', update,
                             idempotency_key=str(uuid.uuid4()), accept='application/json')
    assert status == 200, ('version', status)
    item = json.loads(raw)
    assert len(item['maintenance_history']) == 2 and len(item['review_history']) == 1
    out.append({'role': 'manager', 'path': '/api/management/work-divisions',
                'create_review_version_status': 200, 'comparison': item['comparison']['state'],
                'maintenance_versions': 2, 'review_history': 1})
csv_check('manager', '/api/exports/alert-summary', {'group_by': 'rule'})
csv_check('doctor', '/api/exports/alert-hits', {})
csv_check('doctor', '/api/exports/clinical-records', {
    'scope': {'time_range': {'from': '2020-01-01T00:00:00+08:00', 'to': '2031-01-01T00:00:00+08:00'}},
    'purpose': '合成环境联调核验'})
front = actor('frontdesk')
status, _, _ = front('/api/exports/alert-summary', {'group_by': 'rule'})
assert status == 403
out.append({'role': 'frontdesk', 'path': '/api/exports/alert-summary', 'status': status})
doctor = actor('doctor')
status, _, _ = doctor('/api/management/work-divisions')
assert status == 403
out.append({'role': 'doctor', 'path': '/api/management/work-divisions', 'status': status})
print(json.dumps({'ok': True, 'checks': out}, ensure_ascii=False))
