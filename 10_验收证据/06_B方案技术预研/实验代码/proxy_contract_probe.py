"""Actual loopback proxy session/role/CSRF checks; no clinical body in evidence."""
import json
import time
import uuid
from pathlib import Path
import load_probe as workload
from django.db import connections
from probe.models import Audit, Operation, Record

BASE = 'http://127.0.0.1:18726'
OUTPUT = workload.ROOT / '原始结果/性能定位/proxy_contract_v2.json'
if OUTPUT.exists():
    raise RuntimeError('Refuse to overwrite proxy contract evidence.')
record = Record.objects.filter(encounter__store__code='load_0').order_by('id').first()
foreign = Record.objects.filter(encounter__store__code='load_1').order_by('id').first()
encounter_id, foreign_id, before_version = str(record.encounter_id), str(foreign.encounter_id), record.version
connections.close_all()
checks, server_pids, sessions = [], set(), []
doctor_read_pids = set()
expected_backend_pids = {json.loads((Path.home() / f'.cache/t22-runtime/probe-profile-http-{port}.json').read_text(encoding='utf-8-sig'))['server_pid'] for port in range(18722,18726)}
error = None


def inspect(name, response, expected, clinical=False):
    if response.headers.get('X-PRE-Server-PID'):
        server_pids.add(int(response.headers['X-PRE-Server-PID']))
    valid = response.status_code in expected
    if not clinical:
        valid = valid and '"diagnosis"' not in response.text and '"medications"' not in response.text
    checks.append({'check': name, 'passed': valid, 'http': response.status_code})
    assert valid, name


def login(role):
    session = workload.requests.Session()
    session.trust_env = False
    sessions.append(session)
    first = session.get(BASE + '/probe/login', timeout=15)
    response = session.post(BASE + '/probe/login', json={'username': 'load_0_' + role, 'password': workload.LAB_PASSWORD}, headers={'X-CSRFToken': first.json()['csrf']}, timeout=30)
    inspect('login_' + role, response, {200})
    session.headers['X-CSRFToken'] = session.cookies['csrftoken']
    return session


try:
    anonymous = workload.requests.Session()
    anonymous.trust_env = False
    sessions.append(anonymous)
    inspect('anonymous_clinical_denied', anonymous.get(BASE + '/probe/encounters/' + encounter_id, timeout=15), {401, 403})
    front, manager, doctor = [login(role) for role in ['frontdesk', 'manager', 'doctor']]
    for role, session in [('frontdesk', front), ('manager', manager)]:
        inspect(role + '_clinical_denied', session.get(BASE + '/probe/encounters/' + encounter_id, timeout=15), {403})
    for iteration in range(4):
        response = doctor.get(BASE + '/probe/encounters/' + encounter_id, timeout=15)
        inspect('doctor_owned_session_across_backends_' + str(iteration), response, {200}, clinical=True)
        assert response.json()['record']['encounter_id'] == encounter_id
        doctor_read_pids.add(int(response.headers['X-PRE-Server-PID']))
    shared_session = doctor_read_pids == expected_backend_pids
    checks.append({'check': 'same_doctor_session_success_on_all_expected_backends', 'passed': shared_session})
    assert shared_session
    before_audit_id = Audit.objects.order_by('-id').values_list('id', flat=True).first() or 0
    connections.close_all()
    cross = doctor.get(BASE + '/probe/encounters/' + foreign_id, timeout=15)
    inspect('doctor_cross_store_denied', cross, {403})
    operation_key = uuid.uuid4().hex
    data = {'operation_key': operation_key, 'expected_version': before_version, 'drug': 'PRE proxy', 'amount': '1', 'unit': 'u'}
    response = doctor.post(BASE + '/probe/encounters/' + encounter_id + '/append', json=data, headers={'X-CSRFToken': None}, timeout=15)
    inspect('csrf_missing_write_denied', response, {403})
    response = doctor.post(BASE + '/probe/encounters/' + encounter_id + '/append', json={**data, 'actor_id': 999}, timeout=15)
    inspect('client_actor_override_rejected', response, {422})
    record.refresh_from_db()
    unchanged = record.version == before_version and not Operation.objects.filter(operation_key=operation_key).exists()
    checks.append({'check': 'rejected_requests_no_write_effect', 'passed': unchanged})
    assert unchanged
    denied = list(Audit.objects.filter(id__gt=before_audit_id, actor__username='load_0_doctor', role='doctor', target_ref=foreign_id, action='clinical.read', result='denied', reason_code='SCOPE_DENIED'))
    correct_store = len(denied) == 1 and denied[0].event_store.code == 'load_0'
    checks.append({'check': 'cross_store_denial_audit_requester_store', 'passed': correct_store})
    assert correct_store
except Exception as exc:
    error = type(exc).__name__
    checks.append({'check': 'probe_exception', 'passed': False, 'exception_class': error})
finally:
    for session in sessions:
        session.close()
    connections.close_all()

result = {'kind': 'real_unified_proxy_contract_not_full_acceptance', 'passed': all(c['passed'] for c in checks),
          'checks': checks, 'backend_pids_observed': sorted(server_pids), 'same_doctor_read_pids': sorted(doctor_read_pids),
          'expected_backend_pids': sorted(expected_backend_pids), 'exception_class': error, 'clinical_bodies_saved': False}
OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'passed': result['passed'], 'checks': len(checks), 'backend_pids': len(server_pids)}), flush=True)
raise SystemExit(0 if result['passed'] else 1)
