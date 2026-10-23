"""Closed-loop, complete-body HTTP workload for frozen TBR-05 v1.0."""
import argparse
import csv
import hashlib
import io
import itertools
import json
import math
import os
import platform
import random
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import psutil

NAMESPACE = uuid.UUID('23863255-7eb6-4692-96e2-cb87d8dbf54c')
MIX = (['query'] * 6 + ['guard'] * 3 + ['reception'] * 3 + ['write'] * 4 +
       ['clinical_export'] * 2 + ['detail_export', 'summary_export'])
LOCK = threading.Lock()
SERIAL = itertools.count()
TARGETS = Counter()
ACTIVE = 0
SAMPLES = []
RESOURCES = []
STOP_SAMPLER = threading.Event()


def ref(kind, shop, number):
    return str(uuid.uuid5(NAMESPACE, f'20261024:{kind}:{shop}:{number}'))


def quantile(values, fraction):
    return sorted(values)[math.ceil(fraction * len(values)) - 1] if values else None


def next_task():
    with LOCK:
        number = next(SERIAL)
    return number, MIX[number % len(MIX)]


def next_target(label):
    with LOCK:
        number = TARGETS[label]
        TARGETS[label] += 1
    return number


def monitor(start):
    while not STOP_SAMPLER.is_set():
        with LOCK:
            SAMPLES.append({'seconds': time.perf_counter() - start, 'in_flight': ACTIVE})
        STOP_SAMPLER.wait(0.1)


def monitor_resources(start, server_pid, mysql_pid):
    processes = {'load': psutil.Process(os.getpid())}
    if server_pid:
        processes['web'] = psutil.Process(server_pid)
    if mysql_pid:
        processes['mysql'] = psutil.Process(mysql_pid)
    psutil.cpu_percent(interval=None)
    while not STOP_SAMPLER.is_set():
        sample = {'seconds': time.perf_counter() - start,
                  'host_cpu_percent': psutil.cpu_percent(interval=None),
                  'host_memory_percent': psutil.virtual_memory().percent}
        network = psutil.net_io_counters()
        sample['host_bytes_sent'] = network.bytes_sent
        sample['host_bytes_recv'] = network.bytes_recv
        for name, process in processes.items():
            try:
                sample[name + '_rss_bytes'] = process.memory_info().rss
                cpu = process.cpu_times()
                sample[name + '_cpu_seconds'] = cpu.user + cpu.system
            except psutil.NoSuchProcess:
                sample[name + '_missing'] = True
        RESOURCES.append(sample)
        STOP_SAMPLER.wait(1)


def authentication(base, private):
    sessions = {}
    for account in private['accounts']:
        session = requests.Session()
        home = session.get(base + '/app', timeout=20)
        home.raise_for_status()
        csrf = session.cookies.get('csrftoken')
        response = session.post(base + '/api/login', json={'username': account['username'],
                                'password': private['password']}, headers={'X-CSRFToken': csrf}, timeout=20)
        if response.status_code != 200:
            raise RuntimeError(f"Synthetic account login failed: {account['username']} {response.status_code}")
        sessions[account['username']] = {'sessionid': session.cookies['sessionid'],
                                         'csrftoken': session.cookies['csrftoken']}
        session.close()
    return sessions


def choose_actor(private, role, shop, doctor=None):
    name = f'load_{shop + 1}_{role}_{(doctor or 0) + 1}'
    return name


def select_request(kind, sequence, rng):
    shop = rng.randrange(5)
    number = rng.randrange(4000)
    doctor = 0 if shop == 0 else number % 2
    if kind == 'query':
        actor = choose_actor(None, 'doctor', shop, doctor)
        return actor, 'GET', f'/api/clinical/encounters/{ref("encounter", shop, number)}', None, 'json', None
    if kind == 'reception':
        actor = choose_actor(None, 'frontdesk', shop)
        payload = {'pet_identity_choice': 'new', 'pet_ref': str(uuid.uuid4()),
                   'species': '猫', 'complaint': f'合成压测主诉 {sequence}',
                   'doctor_ref': None, 'pet_name': '合成负载新宠物'}
        return actor, 'POST', '/api/encounters', payload, 'json', shop
    if kind == 'guard':
        target = next_target('guard')
        if target >= 8000:
            raise RuntimeError('Guard target pool exhausted')
        shop = target % 5
        slot = target // 5
        number = 10 * (slot // 4) + (3, 5, 7, 9)[slot % 4]
        doctor = 0 if shop == 0 else number % 2
        payload = {'original_content_ref': ref('record', shop, number),
                   'original_version_ref': ref('revision1', shop, number),
                   'diagnosis': f'合成护栏校验通过 {sequence}', 'reason': '合成负载校验'}
        return choose_actor(None, 'doctor', shop, doctor), 'POST', \
            f'/api/clinical/encounters/{ref("encounter", shop, number)}/diagnosis/corrections', payload, 'json', None
    if kind == 'write':
        subtype = sequence % 5
        if subtype in (0, 1):
            target = next_target('write_diagnosis')
            if target >= 8000:
                raise RuntimeError('Diagnosis target pool exhausted')
            shop = target % 5
            slot = target // 5
            number = 10 * (slot // 4) + (2, 4, 6, 8)[slot % 4]
            doctor = 0 if shop == 0 else number % 2
            payload = {'original_content_ref': ref('record', shop, number),
                       'original_version_ref': ref('revision1', shop, number),
                       'diagnosis': f'合成病历更正 {sequence}', 'reason': '合成负载写入'}
            return choose_actor(None, 'doctor', shop, doctor), 'POST', \
                f'/api/clinical/encounters/{ref("encounter", shop, number)}/diagnosis/corrections', payload, 'json', None
        if subtype in (2, 3):
            payload = {'drug': '合成负载药品', 'amount': '1', 'unit': '片'}
            return choose_actor(None, 'doctor', shop, doctor), 'POST', \
                f'/api/clinical/encounters/{ref("encounter", shop, number)}/medications', payload, 'json', None
        target = next_target('write_adoption')
        if target >= 2000:
            raise RuntimeError('Suggestion target pool exhausted')
        shop = target % 5
        number = 10 * (target // 5) + 1
        doctor = 0 if shop == 0 else number % 2
        payload = {'encounter_ref': ref('encounter', shop, number),
                   'suggestion_ref': ref('suggestion', shop, number),
                   'record_ref': ref('record', shop, number), 'reason': '合成负载人工采纳',
                   'adopted_content': {'original_version_ref': ref('revision1', shop, number),
                                       'medication': {'drug': '合成建议药品', 'amount': '1', 'unit': '片'}}}
        return choose_actor(None, 'doctor', shop, doctor), 'POST', \
            f'/api/clinical/suggestions/{ref("suggestion", shop, number)}/adoption', payload, 'json', None
    if kind in ('clinical_export', 'detail_export'):
        distribution = rng.randrange(10)
        if distribution < 6:
            size = rng.randint(1, 20)
            tier = '1-20'
        elif distribution < 9:
            size = rng.randint(50, 200)
            tier = '50-200'
        else:
            size = rng.randint(1000, 4000)
            tier = '1000-4000'
        if size > 2000 or distribution == 9:
            shop, doctor = 0, 0
        else:
            shop = rng.randrange(5)
            doctor = 0 if shop == 0 else rng.randrange(2)
        if shop != 0 and kind == 'detail_export':
            doctor = 0
        candidates = (list(range(4000)) if shop == 0 else
                      list(range(doctor, 4000, 2)))
        if kind == 'detail_export' and shop != 0:
            candidates = list(range(0, 4000, 2))
        size = min(size, len(candidates))
        chosen = rng.sample(candidates, size)
        actor = choose_actor(None, 'doctor', shop, doctor)
        if kind == 'clinical_export':
            payload = {'scope': {'record_refs': [ref('record', shop, n) for n in chosen]},
                       'purpose': '合成正式负载验证'}
            return actor, 'POST', '/api/exports/clinical-records', payload, tier, size
        payload = {'scope': {'encounter_refs': [ref('encounter', shop, n) for n in chosen]}}
        return actor, 'POST', '/api/exports/alert-hits', payload, tier, size
    actor = choose_actor(None, 'manager', shop)
    return actor, 'POST', '/api/exports/alert-summary', {'group_by': 'rule'}, 'summary', None


def run_one(session, base, cookies, actors, number, kind, rng):
    actor, method, path, payload, tier, expected_count = select_request(kind, number, rng)
    if kind == 'reception':
        payload['doctor_ref'] = actors[f'load_{expected_count + 1}_doctor_1']['account_ref']
    request_cookies = cookies[actor]
    headers = {'Accept': 'text/csv' if 'export' in kind else 'application/json'}
    if method == 'POST':
        headers['X-CSRFToken'] = request_cookies['csrftoken']
        if kind in ('reception', 'guard', 'write'):
            headers['Idempotency-Key'] = str(uuid.uuid4())
    started = time.perf_counter()
    first = None
    global ACTIVE
    with LOCK:
        ACTIVE += 1
    try:
        response = session.request(method, base + path, json=payload if method == 'POST' else None,
                                   cookies=request_cookies, headers=headers, timeout=120, stream=True)
        pieces = []
        for piece in response.iter_content(chunk_size=65536):
            if piece:
                if first is None:
                    first = time.perf_counter()
                pieces.append(piece)
        body = b''.join(pieces)
        ended = time.perf_counter()
        code = None
        rows = None
        digest_match = None
        if response.status_code == 200:
            if 'export' in kind:
                digest_match = (int(response.headers['Content-Length']) == len(body) ==
                                int(response.headers['X-Byte-Count']) and
                                hashlib.sha256(body).hexdigest() == response.headers['X-Content-SHA256'])
                rows = len(list(csv.reader(io.StringIO(body.decode('utf-8'))))) - 1
                valid = digest_match and (expected_count is None or
                                          (rows == expected_count if kind == 'clinical_export' else rows <= expected_count))
            else:
                obj = json.loads(body)
                valid = obj.get('ok') is not False
                if kind == 'reception':
                    valid = valid and bool(obj.get('encounter_ref'))
        else:
            valid = False
            try:
                code = json.loads(body).get('error', {}).get('code')
            except (ValueError, UnicodeError):
                code = 'NON_JSON_ERROR'
        return {'request_id': number, 'kind': kind, 'tier': tier, 'status': response.status_code,
                'code': code, 'ok': bool(valid), 'started': started, 'first': first, 'ended': ended,
                'bytes': len(body), 'rows': rows, 'sha256_match': digest_match,
                'expected_count': expected_count if 'export' in kind else None,
                'server_ref': response.headers.get('X-Export-Ref')}
    except Exception as error:
        return {'request_id': number, 'kind': kind, 'tier': tier, 'status': None,
                'code': type(error).__name__, 'ok': False, 'started': started, 'first': first,
                'ended': time.perf_counter(), 'bytes': 0, 'rows': None, 'sha256_match': None,
                'expected_count': expected_count if 'export' in kind else None, 'server_ref': None}
    finally:
        with LOCK:
            ACTIVE -= 1


def worker(index, gate, end_at, base, cookies, actors, results):
    rng = random.Random(20261024 + index)
    session = requests.Session()
    gate.wait(timeout=180)
    while time.perf_counter() < end_at:
        number, kind = next_task()
        result = run_one(session, base, cookies, actors, number, kind, rng)
        results.append(result)
    session.close()


def summarize(rows, samples, start, end, warmup):
    formal = [r for r in rows if start + warmup <= r['started'] < end]
    groups = {}
    for kind in sorted(set(MIX)):
        selected = [r for r in formal if r['kind'] == kind]
        times = [(r['ended'] - r['started']) * 1000 for r in selected]
        groups[kind] = {'count': len(selected), 'failures': sum(not r['ok'] for r in selected),
                        'p50_ms': quantile(times, .5), 'p95_ms': quantile(times, .95),
                        'p99_ms': quantile(times, .99), 'max_ms': max(times, default=None)}
    minutes = []
    for minute in range(10):
        left, right = start + warmup + minute * 60, start + warmup + (minute + 1) * 60
        selected = [r for r in formal if left <= r['ended'] < right]
        minutes.append({'minute': minute + 1, 'completed': len(selected),
                        'success': sum(r['ok'] for r in selected), 'failures': sum(not r['ok'] for r in selected)})
    during = [s['in_flight'] for s in samples if warmup <= s['seconds'] < end - start]
    return {'kind': 'formal' if warmup == 300 and end - start >= 900 else 'preflight_not_AC06',
            'planned_seconds': end - start, 'warmup_seconds': warmup,
            'formal_requests_started': len(formal), 'formal_failures': sum(not r['ok'] for r in formal),
            'groups': groups, 'minutes': minutes, 'in_flight_min': min(during, default=None),
            'in_flight_p50': quantile(during, .5), 'in_flight_p95': quantile(during, .95),
            'in_flight_max': max(during, default=None),
            'actual_mix': {kind: groups[kind]['count'] / len(formal) if formal else 0 for kind in groups}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:18830')
    parser.add_argument('--private-accounts', type=Path, required=True)
    parser.add_argument('--output-prefix', type=Path, required=True)
    parser.add_argument('--concurrency', type=int, default=360)
    parser.add_argument('--warmup-seconds', type=int, default=300)
    parser.add_argument('--formal-seconds', type=int, default=600)
    parser.add_argument('--server-pid', type=int)
    parser.add_argument('--mysql-pid', type=int)
    args = parser.parse_args()
    if args.concurrency < 1 or args.output_prefix.with_suffix('.summary.json').exists():
        parser.error('Invalid concurrency or existing result; results are never overwritten')
    private = json.loads(args.private_accounts.read_text(encoding='utf-8'))
    if not private.get('synthetic_only') or private['manifest']['seed'] != 20261024:
        parser.error('Expected only the deterministic synthetic fixture')
    actors = {a['username']: a for a in private['accounts']}
    cookies = authentication(args.base.rstrip('/'), private)
    total = args.warmup_seconds + args.formal_seconds
    start = time.perf_counter()
    end = start + total
    gate = threading.Barrier(args.concurrency + 1)
    results = []
    watcher = threading.Thread(target=monitor, args=(start,), daemon=True)
    watcher.start()
    resource_watcher = threading.Thread(target=monitor_resources,
                                        args=(start, args.server_pid, args.mysql_pid), daemon=True)
    resource_watcher.start()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(worker, n, gate, end, args.base.rstrip('/'), cookies, actors, results)
                   for n in range(args.concurrency)]
        gate.wait(timeout=180)
        for future in futures:
            future.result()
    STOP_SAMPLER.set()
    watcher.join(timeout=5)
    resource_watcher.join(timeout=5)
    prefix = args.output_prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    with prefix.with_suffix('.requests.jsonl').open('x', encoding='utf-8') as stream:
        for row in results:
            item = {**row, 'started': row['started'] - start,
                    'first': row['first'] - start if row['first'] else None,
                    'ended': row['ended'] - start}
            stream.write(json.dumps(item, ensure_ascii=False) + '\n')
    summary = summarize(results, SAMPLES, start, end, args.warmup_seconds)
    if args.concurrency != 360:
        summary['kind'] = 'preflight_not_AC06'
    summary.update({'concurrency': args.concurrency, 'sample_count': len(SAMPLES),
                    'actual_duration_seconds': time.perf_counter() - start,
                    'resource_samples': len(RESOURCES), 'os': platform.platform(),
                    'python': platform.python_version(), 'requests': requests.__version__,
                    'psutil': psutil.__version__,
                    'resource_peaks': {key: max((row.get(key, 0) for row in RESOURCES), default=None)
                                       for key in ('host_cpu_percent', 'host_memory_percent',
                                                   'web_rss_bytes', 'mysql_rss_bytes', 'load_rss_bytes')}})
    prefix.with_suffix('.summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    prefix.with_suffix('.inflight.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in SAMPLES), encoding='utf-8')
    prefix.with_suffix('.resources.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in RESOURCES), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
