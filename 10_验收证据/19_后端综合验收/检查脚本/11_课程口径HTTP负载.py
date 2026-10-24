"""360 active sessions with declared pacing; separate from frozen TBR-05."""
import argparse
import csv
import hashlib
import io
import json
import math
import os
import random
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil
import requests

NAMESPACE = uuid.UUID('23863255-7eb6-4692-96e2-cb87d8dbf54c')
LOCK = threading.Lock()
TARGETS = Counter()
ACTIVE = 0
STOP = threading.Event()
SAMPLES = []
RESOURCES = []
KIND_WEIGHTS = {'query': 35, 'guard': 15, 'write': 25,
                'clinical_export': 2, 'detail_export': 1}


def ref(kind, shop, number):
    return str(uuid.uuid5(NAMESPACE, f'20261024:{kind}:{shop}:{number}'))


def percentile(values, part):
    return sorted(values)[math.ceil(len(values) * part) - 1] if values else None


def persona_pool(accounts):
    fronts = [item for item in accounts if item['role'] == 'frontdesk']
    doctors = [item for item in accounts if item['role'] == 'doctor' and
               item['username'] != 'load_1_doctor_2']
    managers = [item for item in accounts if item['role'] == 'manager']
    if (len(fronts), len(doctors), len(managers)) != (5, 9, 5):
        raise ValueError('Unexpected synthetic account distribution')
    people = ([fronts[index % 5] for index in range(72)] +
              [doctors[index % 9] for index in range(281)] +
              [managers[index % 5] for index in range(7)])
    random.Random(20261025).shuffle(people)
    return people


def login(base, account, password):
    session = requests.Session()
    home = session.get(base + '/app', timeout=30)
    home.raise_for_status()
    token = session.cookies.get('csrftoken')
    if not token:
        raise RuntimeError('CSRF preparation failed')
    response = session.post(base + '/api/login', json={'username': account['username'],
                            'password': password}, headers={'X-CSRFToken': token}, timeout=30)
    if response.status_code != 200 or not session.cookies.get('sessionid'):
        raise RuntimeError('Synthetic session login failed')
    return session


def position(account):
    words = account['username'].split('_')
    return int(words[1]) - 1, int(words[-1]) - 1


def eligible_numbers(account):
    shop, doctor = position(account)
    return tuple(number for number in range(4000) if shop == 0 or number % 2 == doctor)


def next_target(kind, account, choices):
    key = (kind, account['username'])
    with LOCK:
        index = TARGETS[key]
        TARGETS[key] += 1
    if index >= len(choices):
        raise RuntimeError('Unique synthetic correction targets exhausted')
    return choices[index]


def request_spec(account, number, rng, pools):
    shop, _ = position(account)
    role = account['role']
    if role == 'frontdesk':
        return 'reception', 'POST', '/api/encounters', {
            'pet_identity_choice': 'new', 'pet_ref': str(uuid.uuid4()),
            'species': '猫', 'complaint': f'合成压测主诉 {number}',
            'doctor_ref': pools['doctor_ids'][shop][rng.randrange(2)],
            'pet_name': '合成负载新宠物'}, 'json', None
    if role == 'manager':
        return 'summary_export', 'POST', '/api/exports/alert-summary', {'group_by': 'rule'}, 'summary', None

    kind = rng.choices(list(KIND_WEIGHTS), weights=list(KIND_WEIGHTS.values()))[0]
    choices = pools[account['username']]
    if kind == 'query':
        target = rng.choice(choices['all'])
        return kind, 'GET', f'/api/clinical/encounters/{ref("encounter", shop, target)}', None, 'json', None
    if kind in ('guard', 'write'):
        target = next_target(kind, account, choices[kind])
        payload = {'original_content_ref': ref('record', shop, target),
                   'original_version_ref': ref('revision1', shop, target),
                   'diagnosis': f'合成{kind}更正 {number}', 'reason': '合成负载校验'}
        return kind, 'POST', f'/api/clinical/encounters/{ref("encounter", shop, target)}/diagnosis/corrections', payload, 'json', None

    size = rng.randint(1, 20) if rng.randrange(10) < 9 else rng.randint(21, 100)
    targets = rng.sample(choices['all'], size)
    tier = '1-20' if size <= 20 else '21-100'
    if kind == 'clinical_export':
        payload = {'scope': {'record_refs': [ref('record', shop, target) for target in targets]},
                   'purpose': '合成课程口径负载验证'}
        return kind, 'POST', '/api/exports/clinical-records', payload, tier, size
    payload = {'scope': {'encounter_refs': [ref('encounter', shop, target) for target in targets]}}
    return kind, 'POST', '/api/exports/alert-hits', payload, tier, size


def request_one(session, account, base, number, rng, pools, worker_id):
    started = time.perf_counter()
    first = None
    kind = 'unselected'
    tier = None
    global ACTIVE
    try:
        kind, method, path, payload, tier, expected = request_spec(account, number, rng, pools)
        headers = {'Accept': 'text/csv' if 'export' in kind else 'application/json'}
        if method == 'POST':
            headers['X-CSRFToken'] = session.cookies['csrftoken']
            if kind in ('reception', 'guard', 'write'):
                headers['Idempotency-Key'] = str(uuid.uuid4())
        with LOCK:
            ACTIVE += 1
        try:
            response = session.request(method, base + path, json=payload if method == 'POST' else None,
                                       headers=headers, timeout=120, stream=True)
            body = bytearray()
            for piece in response.iter_content(chunk_size=65536):
                if piece:
                    first = first or time.perf_counter()
                    body.extend(piece)
            ended = time.perf_counter()
            valid = response.status_code == 200
            rows = None
            code = None
            digest_ok = None
            if valid and 'export' in kind:
                raw = bytes(body)
                rows = sum(1 for _ in csv.reader(io.StringIO(raw.decode('utf-8')))) - 1
                digest_ok = (int(response.headers['Content-Length']) == len(raw) ==
                             int(response.headers['X-Byte-Count']) and
                             hashlib.sha256(raw).hexdigest() == response.headers['X-Content-SHA256'])
                valid = digest_ok and (kind != 'clinical_export' or rows == expected)
            elif valid:
                result = json.loads(body)
                valid = result.get('ok') is not False
                if kind == 'reception':
                    valid = valid and bool(result.get('encounter_ref'))
            else:
                try:
                    code = json.loads(body).get('error', {}).get('code')
                except (ValueError, UnicodeError):
                    code = 'NON_JSON_ERROR'
            return {'worker': worker_id, 'kind': kind, 'tier': tier, 'started': started,
                    'first': first, 'ended': ended, 'status': response.status_code,
                    'code': code, 'ok': bool(valid), 'bytes': len(body), 'rows': rows,
                    'sha256_match': digest_ok,
                    'server_timing': response.headers.get('Server-Timing'),
                    'sql_count': response.headers.get('X-PRE-SQL-Count')}
        finally:
            with LOCK:
                ACTIVE -= 1
    except Exception as error:
        return {'worker': worker_id, 'kind': kind, 'tier': tier, 'started': started,
                'first': first, 'ended': time.perf_counter(), 'status': None,
                'code': type(error).__name__, 'ok': False, 'bytes': 0, 'rows': None,
                'sha256_match': None, 'server_timing': None, 'sql_count': None}


def worker(index, session, account, base, pools, gate, start, end, results):
    rng = random.Random(20261025 + index)
    gate.wait(timeout=180)
    due = start + rng.uniform(0, 20)
    count = 0
    while due < end:
        delay = due - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        if time.perf_counter() >= end:
            break
        result = request_one(session, account, base, index * 100000 + count, rng, pools, index)
        results.append(result)
        count += 1
        due = result['ended'] + rng.uniform(15, 25)
    session.close()


def monitor(start, server_pid, mysql_pid):
    processes = {'client': psutil.Process(os.getpid())}
    for name, pid in [('web', server_pid), ('mysql', mysql_pid)]:
        if pid:
            processes[name] = psutil.Process(pid)
    psutil.cpu_percent(interval=None)
    while not STOP.is_set():
        with LOCK:
            SAMPLES.append({'seconds': time.perf_counter() - start, 'in_flight': ACTIVE})
        if len(SAMPLES) % 10 == 0:
            row = {'seconds': time.perf_counter() - start,
                   'host_cpu_percent': psutil.cpu_percent(interval=None),
                   'host_memory_percent': psutil.virtual_memory().percent}
            for name, process in processes.items():
                try:
                    row[name + '_rss_bytes'] = process.memory_info().rss
                    times = process.cpu_times()
                    row[name + '_cpu_seconds'] = times.user + times.system
                except psutil.NoSuchProcess:
                    row[name + '_missing'] = True
            RESOURCES.append(row)
        STOP.wait(0.1)


def summarize(results, start, warmup, duration, unique_sessions, account_count):
    left, end = start + warmup, start + warmup + duration
    formal = [row for row in results if left <= row['started'] < end]
    groups = {}
    for kind in ('query', 'guard', 'reception', 'write', 'clinical_export', 'detail_export', 'summary_export'):
        selected = [row for row in formal if row['kind'] == kind]
        timings = [1000 * (row['ended'] - row['started']) for row in selected]
        groups[kind] = {'count': len(selected), 'failures': sum(not row['ok'] for row in selected),
                        'p50_ms': percentile(timings, .5), 'p95_ms': percentile(timings, .95),
                        'p99_ms': percentile(timings, .99), 'max_ms': max(timings, default=None)}
    minutes = []
    for minute in range(math.ceil(duration / 60)):
        selected = [row for row in formal if left + minute * 60 <= row['ended'] < left + (minute + 1) * 60]
        minutes.append({'minute': minute + 1, 'completed': len(selected),
                        'success': sum(row['ok'] for row in selected),
                        'failures': sum(not row['ok'] for row in selected)})
    active = [row['in_flight'] for row in SAMPLES if warmup <= row['seconds'] < warmup + duration]
    participants = {row['worker'] for row in formal}
    return {'kind': 'course_360_active_sessions' if duration == 600 and warmup == 60 else 'preflight_not_AC06',
            'active_sessions': 360, 'distinct_login_sessions': unique_sessions,
            'formal_participating_sessions': len(participants), 'participating_business_accounts': account_count,
            'fixture_business_accounts': 20,
            'warmup_seconds': warmup, 'formal_seconds': duration,
            'formal_requests_started': len(formal), 'formal_failures': sum(not row['ok'] for row in formal),
            'groups': groups, 'minutes': minutes,
            'in_flight_min': min(active, default=None), 'in_flight_p50': percentile(active, .5),
            'in_flight_p95': percentile(active, .95), 'in_flight_max': max(active, default=None),
            'actual_mix': {kind: groups[kind]['count'] / len(formal) if formal else 0 for kind in groups},
            'resource_peaks': {key: max((row.get(key, 0) for row in RESOURCES), default=None)
                               for key in ('host_cpu_percent', 'host_memory_percent', 'web_rss_bytes',
                                           'mysql_rss_bytes', 'client_rss_bytes')}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:18830')
    parser.add_argument('--private-accounts', type=Path, required=True)
    parser.add_argument('--output-prefix', type=Path, required=True)
    parser.add_argument('--warmup-seconds', type=int, default=60)
    parser.add_argument('--formal-seconds', type=int, default=600)
    parser.add_argument('--target-offset', type=int, default=0,
                        help='Diagnostic reuse of a previously mutated fixture; formal runs require zero')
    parser.add_argument('--server-pid', type=int)
    parser.add_argument('--mysql-pid', type=int)
    args = parser.parse_args()
    if (args.warmup_seconds < 0 or args.formal_seconds < 1 or args.target_offset < 0 or
            args.output_prefix.with_suffix('.summary.json').exists()):
        parser.error('Invalid duration or existing output; evidence is never overwritten')
    private = json.loads(args.private_accounts.read_text(encoding='utf-8'))
    if not private.get('synthetic_only') or private['manifest']['seed'] != 20261024:
        parser.error('Expected only the deterministic synthetic fixture')
    people = persona_pool(private['accounts'])
    accounts = {item['username']: item for item in private['accounts']}
    doctor_ids = {shop: [accounts[f'load_{shop + 1}_doctor_{person + 1}']['account_ref']
                         for person in range(2)] for shop in range(5)}
    pools = {'doctor_ids': doctor_ids}
    for person in people:
        if person['role'] == 'doctor' and person['username'] not in pools:
            numbers = eligible_numbers(person)
            pools[person['username']] = {
                'all': numbers,
                'guard': tuple(value for value in numbers if value % 20 in (3, 5, 7, 9, 12, 14, 16, 18)),
                'write': tuple(value for value in numbers if value % 20 in (1, 2, 4, 6, 8, 11, 13, 15, 17, 19))}
            for kind in ('guard', 'write'):
                if args.target_offset >= len(pools[person['username']][kind]):
                    parser.error('Target offset exceeds synthetic target pool')
                TARGETS[(kind, person['username'])] = args.target_offset
    with ThreadPoolExecutor(max_workers=12) as preparation:
        sessions = list(preparation.map(
            lambda person: login(args.base.rstrip('/'), person, private['password']), people))
    unique_sessions = len({session.cookies['sessionid'] for session in sessions})
    if unique_sessions != 360:
        raise RuntimeError('Distinct login sessions were not established')
    start = time.perf_counter()
    end = start + args.warmup_seconds + args.formal_seconds
    gate = threading.Barrier(361)
    results = []
    watcher = threading.Thread(target=monitor, args=(start, args.server_pid, args.mysql_pid), daemon=True)
    watcher.start()
    try:
        with ThreadPoolExecutor(max_workers=360) as pool:
            futures = [pool.submit(worker, index, session, person, args.base.rstrip('/'), pools,
                                   gate, start, end, results)
                       for index, (session, person) in enumerate(zip(sessions, people))]
            gate.wait(timeout=180)
            for future in futures:
                future.result()
    finally:
        STOP.set()
        watcher.join(timeout=5)
    summary = summarize(results, start, args.warmup_seconds, args.formal_seconds, unique_sessions,
                        len({person['username'] for person in people}))
    summary['target_offset'] = args.target_offset
    if args.target_offset:
        summary['kind'] = 'diagnostic_preflight_not_AC06'
    prefix = args.output_prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    for suffix, rows in (('.requests.jsonl', results), ('.inflight.jsonl', SAMPLES), ('.resources.jsonl', RESOURCES)):
        with prefix.with_suffix(suffix).open('x', encoding='utf-8') as file:
            for row in rows:
                safe = dict(row)
                for field in ('started', 'first', 'ended'):
                    if safe.get(field) is not None:
                        safe[field] -= start
                file.write(json.dumps(safe, ensure_ascii=False) + '\n')
    prefix.with_suffix('.summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
