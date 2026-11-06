"""Closed-loop, complete-body HTTP workload for frozen TBR-05 v1.0."""
import argparse
from array import array
from bisect import bisect_right
import csv
import gc
import hashlib
import io
import itertools
import json
import math
import os
import platform
import queue
import random
import re
import threading
import sys
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import psutil

NAMESPACE = uuid.UUID('23863255-7eb6-4692-96e2-cb87d8dbf54c')
# One random namespace per process, before the measured request loop.
REQUEST_NAMESPACE = uuid.uuid4()
MIX = (['query'] * 6 + ['guard'] * 3 + ['reception'] * 3 + ['write'] * 4 +
       ['clinical_export'] * 2 + ['detail_export', 'summary_export'])
LOCK = threading.Lock()
SERIAL_LOCK = threading.Lock()
TARGET_LOCK = threading.Lock()
SERIAL = itertools.count()
TARGETS = Counter()
ACTIVE = 0
SAMPLES = []
RESOURCES = []
STOP_SAMPLER = threading.Event()
VERSION_BY_TARGET = {}
VERSION_LOCK = threading.Lock()
GUARD_TARGETS = 6670
DIAGNOSIS_TARGETS = 6665
ADOPTION_RECORDS = 6665
ADOPTION_TARGETS = ADOPTION_RECORDS * 16
REPO_ROOT = Path(__file__).resolve().parents[4]
SUMMARY_HITS_BY_STORE = {}
CLIENT_SHARD_COUNT = 1
CLIENT_SHARD_INDEX = 0
TASK_ID_STRIDE = 10 ** 12  # Multiple of 20 preserves the mix and write-subtype phase.


@contextmanager
def gil_switch_interval(milliseconds):
    if milliseconds is not None and (not math.isfinite(milliseconds) or milliseconds <= 0):
        raise ValueError('GIL switch interval must be finite and positive')
    original = sys.getswitchinterval()
    if milliseconds is not None:
        sys.setswitchinterval(milliseconds / 1000)
    try:
        yield sys.getswitchinterval()
    finally:
        if milliseconds is not None:
            sys.setswitchinterval(original)


class GCPauseRecorder:
    """Optional GC timeline; callbacks buffer primitives without disk or queue IO."""

    def __init__(self, prefix, start, started_at_utc, enabled):
        self.path = prefix.with_suffix('.gc.json')
        self.start = start
        self.started_at_utc = started_at_utc
        self.enabled = enabled
        self.events = []
        self.callback = self.capture

    def capture(self, phase, info):
        self.events.append((time.perf_counter(), phase, info['generation'],
                            info.get('collected', 0), info.get('uncollectable', 0)))

    def __enter__(self):
        if self.enabled:
            self.output = self.path.open('x', encoding='utf-8')
            gc.callbacks.append(self.callback)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.enabled:
            gc.callbacks.remove(self.callback)
            with self.output:
                json.dump([{'t': stamp - self.start,
                            'timestamp_utc': utc_at(self.started_at_utc, stamp - self.start),
                            'phase': phase, 'generation': generation,
                            'collected': collected, 'uncollectable': uncollectable}
                           for stamp, phase, generation, collected, uncollectable in self.events],
                          self.output, ensure_ascii=False)


class JsonlRunRecorder:
    """Stream run evidence to disk so an interrupted run keeps completed samples."""

    def __init__(self, prefix):
        self.paths = {
            'requests': prefix.with_suffix('.requests.jsonl'),
            'inflight': prefix.with_suffix('.inflight.jsonl'),
            'resources': prefix.with_suffix('.resources.jsonl'),
        }
        outputs = [*self.paths.values(), prefix.with_suffix('.summary.json')]
        existing = [path for path in outputs if path.exists()]
        if existing:
            raise FileExistsError(f'Results are never overwritten: {existing[0]}')
        prefix.parent.mkdir(parents=True, exist_ok=True)
        self.start = None
        self.started_at_utc = None
        self.events = queue.Queue()
        self.stop = object()
        self.error = None
        self.files = {}
        created = []
        try:
            for name, path in self.paths.items():
                self.files[name] = path.open('x', encoding='utf-8')
                created.append(path)
        except Exception:
            for stream in self.files.values():
                stream.close()
            for path in created:
                path.unlink(missing_ok=True)
            raise
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def set_clock(self, start, started_at_utc):
        self.start = start
        self.started_at_utc = started_at_utc

    def request(self, row):
        self._put('requests', row)

    def sample(self, kind, row):
        if kind not in ('inflight', 'resources'):
            raise ValueError('Unknown run sample kind')
        self._put(kind, row)

    def _put(self, kind, row):
        if self.error:
            raise RuntimeError('Run evidence writer failed') from self.error
        self.events.put((kind, row))

    def _write(self):
        last_flush = time.monotonic()
        try:
            while True:
                try:
                    item = self.events.get(timeout=0.25)
                except queue.Empty:
                    item = None
                if item is self.stop:
                    self.events.task_done()
                    break
                if item is not None:
                    kind, row = item
                    if kind == 'requests':
                        row = {**row, 'started': row['started'] - self.start,
                               'first': row['first'] - self.start if row['first'] else None,
                               'ended': row['ended'] - self.start,
                               **request_timestamps(row, self.start, self.started_at_utc)}
                    self.files[kind].write(json.dumps(row, ensure_ascii=False) + '\n')
                    self.events.task_done()
                if time.monotonic() - last_flush >= 0.25:
                    for stream in self.files.values():
                        stream.flush()
                    last_flush = time.monotonic()
        except Exception as error:
            self.error = error
        finally:
            for stream in self.files.values():
                try:
                    stream.flush()
                except Exception as error:
                    self.error = self.error or error

    def close(self):
        self.events.put(self.stop)
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise TimeoutError('Run evidence writer did not stop')
        for stream in self.files.values():
            stream.close()
        if self.error:
            raise RuntimeError('Run evidence writer failed') from self.error


def utc_at(origin, elapsed):
    return (origin + timedelta(seconds=elapsed)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def request_timestamps(row, start, origin):
    def timestamp(key):
        value = row.get(key)
        return utc_at(origin, value - start) if value is not None else None

    return {'started_at_utc': timestamp('started'),
            'first_byte_at_utc': timestamp('first'),
            'ended_at_utc': timestamp('ended')}


def ref(kind, shop, number):
    return str(uuid.uuid5(NAMESPACE, f'20261024:{kind}:{shop}:{number}'))


# Fixed fixture identifiers only; built before authentication and the timed window.
EXPORT_REFS = {(kind, shop): tuple(ref(kind, shop, n) for n in range(4000))
               for kind in ('record', 'encounter') for shop in range(5)}


def quantile(values, fraction):
    return sorted(values)[math.ceil(fraction * len(values)) - 1] if values else None


def partition_size(total, index, count):
    if type(count) is not int or type(index) is not int or not 1 <= count <= total or not 0 <= index < count:
        raise ValueError('Invalid client target partition')
    return (total - 1 - index) // count + 1


def partition_target(number, total, index, count):
    return index + number % partition_size(total, index, count) * count


def adoption_target(number, index, count):
    records = partition_size(ADOPTION_RECORDS, index, count)
    slot, local_record = divmod(number, records)
    if slot >= ADOPTION_TARGETS // ADOPTION_RECORDS:
        raise RuntimeError('Suggestion target pool exhausted')
    return slot, index + local_record * count


def configure_client_shard(count, index):
    partition_size(min(GUARD_TARGETS, DIAGNOSIS_TARGETS, ADOPTION_RECORDS), index, count)
    global CLIENT_SHARD_COUNT, CLIENT_SHARD_INDEX, SERIAL
    CLIENT_SHARD_COUNT, CLIENT_SHARD_INDEX = count, index
    base = index * TASK_ID_STRIDE
    SERIAL = itertools.count(base)
    return base


def read_common_clock(path, shard_count, concurrency, warmup, formal):
    raw = path.read_bytes()
    clock = json.loads(raw)
    expected = {'clock_version': 1, 'shard_count': shard_count,
                'concurrency': concurrency * shard_count,
                'warmup_seconds': warmup, 'formal_seconds': formal}
    if any(type(clock.get(key)) is not int or clock[key] != value for key, value in expected.items()):
        raise ValueError('Shared clock run parameters mismatch')
    start = clock.get('monotonic_start')
    if type(start) not in (int, float) or not math.isfinite(start) or start < 0:
        raise ValueError('Shared clock requires a finite monotonic origin')
    utc = clock.get('utc_start')
    if not isinstance(utc, str) or datetime.fromisoformat(utc.replace('Z', '+00:00')).tzinfo is None:
        raise ValueError('Shared clock requires a timezone-aware UTC origin')
    return {**clock, 'sha256': hashlib.sha256(raw).hexdigest()}


def next_task():
    with SERIAL_LOCK:
        number = next(SERIAL)
    return number, MIX[number % len(MIX)]


def resolve_target_bases(args):
    if args.target_base and args.tunnel_base:
        raise ValueError('Use either --target-base or legacy --tunnel-base, not both')
    configured = args.target_base or args.tunnel_base or [args.base]
    return [base.rstrip('/') for base in configured]


def target_worker_assignments(concurrency, target_count, worker_offset=0):
    if target_count < 1:
        raise ValueError('At least one target endpoint is required')
    return [sum(1 for worker in range(worker_offset, worker_offset + concurrency) if worker % target_count == target)
            for target in range(target_count)]


def next_target(label):
    with TARGET_LOCK:
        number = TARGETS[label]
        TARGETS[label] += 1
    return number


def current_version_ref(shop, number):
    key = (shop, number)
    with VERSION_LOCK:
        return VERSION_BY_TARGET.setdefault(key, ref('revision2' if number % 20 == 0 else 'revision1', shop, number))


def update_current_version(key, item):
    versions = item.get('clinical_versions') or []
    version_ref = versions[-1].get('version_ref') if versions else None
    if key and version_ref:
        with VERSION_LOCK:
            VERSION_BY_TARGET[key] = version_ref
        return True
    return False


def monitor(start, started_at_utc, recorder=None, pending=None):
    while not STOP_SAMPLER.is_set():
        elapsed = time.perf_counter() - start
        with LOCK:
            active = ACTIVE
        sample = {'seconds': elapsed, 'timestamp_utc': utc_at(started_at_utc, elapsed),
                  'in_flight': active}
        if pending is not None:
            sample['validation_pending'] = pending.qsize()
        SAMPLES.append(sample)
        if recorder:
            recorder.sample('inflight', sample)
        STOP_SAMPLER.wait(0.1)


def monitor_resources(start, started_at_utc, server_pid, mysql_pid, recorder=None):
    processes = {'load': psutil.Process(os.getpid())}
    if server_pid:
        processes['web'] = psutil.Process(server_pid)
    if mysql_pid:
        processes['mysql'] = psutil.Process(mysql_pid)
    psutil.cpu_percent(interval=None)
    while not STOP_SAMPLER.is_set():
        elapsed = time.perf_counter() - start
        sample = {'seconds': elapsed, 'timestamp_utc': utc_at(started_at_utc, elapsed),
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
        if recorder:
            recorder.sample('resources', sample)
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


def request_ref(purpose, sequence):
    return str(uuid.uuid5(REQUEST_NAMESPACE, f'{purpose}:{sequence}'))


def select_request(kind, sequence, rng):
    shop = rng.randrange(5)
    number = rng.randrange(4000)
    doctor = 0 if shop == 0 else number % 2
    if kind == 'query':
        actor = choose_actor(None, 'doctor', shop, doctor)
        return actor, 'GET', f'/api/clinical/encounters/{ref("encounter", shop, number)}', None, 'json', None
    if kind == 'reception':
        actor = choose_actor(None, 'frontdesk', shop)
        payload = {'pet_identity_choice': 'new', 'pet_ref': request_ref('pet', sequence),
                   'species': '猫', 'complaint': f'合成压测主诉 {sequence}',
                   'doctor_ref': None, 'pet_name': '合成负载新宠物'}
        return actor, 'POST', '/api/encounters', payload, 'json', shop
    if kind == 'guard':
        target = partition_target(next_target('guard'), GUARD_TARGETS, CLIENT_SHARD_INDEX, CLIENT_SHARD_COUNT)
        shop = target % 5
        number = 3 * (target // 5)
        doctor = 0 if shop == 0 else number % 2
        payload = {'original_content_ref': ref('record', shop, number),
                   'original_version_ref': current_version_ref(shop, number),
                   'diagnosis': f'合成护栏校验通过 {sequence}', 'reason': '合成负载校验',
                   '_load_target_key': (shop, number)}
        return choose_actor(None, 'doctor', shop, doctor), 'POST', \
            f'/api/clinical/encounters/{ref("encounter", shop, number)}/diagnosis/corrections', payload, 'json', None
    if kind == 'write':
        subtype = sequence % 5
        if subtype in (0, 1):
            target = partition_target(next_target('write_diagnosis'), DIAGNOSIS_TARGETS, CLIENT_SHARD_INDEX, CLIENT_SHARD_COUNT)
            shop = target % 5
            number = 3 * (target // 5) + 1
            doctor = 0 if shop == 0 else number % 2
            payload = {'original_content_ref': ref('record', shop, number),
                       'original_version_ref': current_version_ref(shop, number),
                       'diagnosis': f'合成病历更正 {sequence}', 'reason': '合成负载写入',
                       '_load_target_key': (shop, number)}
            return choose_actor(None, 'doctor', shop, doctor), 'POST', \
                f'/api/clinical/encounters/{ref("encounter", shop, number)}/diagnosis/corrections', payload, 'json', None
        if subtype in (2, 3):
            payload = {'drug': '合成负载药品', 'amount': '1', 'unit': '片'}
            return choose_actor(None, 'doctor', shop, doctor), 'POST', \
                f'/api/clinical/encounters/{ref("encounter", shop, number)}/medications', payload, 'json', None
        slot, target = adoption_target(next_target('write_adoption'), CLIENT_SHARD_INDEX, CLIENT_SHARD_COUNT)
        shop = target % 5
        number = 3 * (target // 5) + 2
        suggestion_ref = ref('suggestion' if slot == 0 else f'suggestion-{slot}', shop, number)
        doctor = 0 if shop == 0 else number % 2
        payload = {'encounter_ref': ref('encounter', shop, number),
                   'suggestion_ref': suggestion_ref,
                   'record_ref': ref('record', shop, number), 'reason': '合成负载人工采纳',
                   'adopted_content': {'original_version_ref': current_version_ref(shop, number),
                                       'medication': {'drug': '合成建议药品', 'amount': '1', 'unit': '片'}}}
        return choose_actor(None, 'doctor', shop, doctor), 'POST', \
            f'/api/clinical/suggestions/{suggestion_ref}/adoption', payload, 'json', None
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
            payload = {'scope': {'record_refs': [EXPORT_REFS['record', shop][n] for n in chosen]},
                       'purpose': '合成正式负载验证'}
            return actor, 'POST', '/api/exports/clinical-records', payload, tier, size
        payload = {'scope': {'encounter_refs': [EXPORT_REFS['encounter', shop][n] for n in chosen]}}
        return actor, 'POST', '/api/exports/alert-hits', payload, tier, size
    actor = choose_actor(None, 'manager', shop)
    return actor, 'POST', '/api/exports/alert-summary', {'group_by': 'rule'}, 'summary', None


def run_one(session, base, cookies, actors, number, kind, rng):
    preparing = time.perf_counter()
    actor, method, path, payload, tier, expected_count = select_request(kind, number, rng)
    selected = time.perf_counter()
    target_key = payload.pop('_load_target_key', None) if payload else None
    if kind == 'reception':
        payload['doctor_ref'] = actors[f'load_{expected_count + 1}_doctor_1']['account_ref']
    if kind == 'summary_export':
        expected_count = SUMMARY_HITS_BY_STORE[actors[actor]['store_ref']]
    request_cookies = cookies[actor]
    headers = {'Accept': 'text/csv' if 'export' in kind else 'application/json'}
    if method == 'POST':
        headers['X-CSRFToken'] = request_cookies['csrftoken']
        if kind in ('reception', 'guard', 'write'):
            headers['Idempotency-Key'] = request_ref('idempotency', number)
    started = time.perf_counter()
    preparation = {'client_select_ms': (selected - preparing) * 1000,
                   'client_headers_ms': (started - selected) * 1000}
    first = None
    response = None
    result = None
    global ACTIVE
    with LOCK:
        ACTIVE += 1
    active = True
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
        with LOCK:
            ACTIVE -= 1
        active = False
        result = {'request_id': number, 'kind': kind, 'tier': tier, 'status': response.status_code,
                  'started': started, 'first': first, 'ended': ended, 'bytes': len(body),
                  'expected_count': expected_count if 'export' in kind else None,
                  'server_ref': response.headers.get('X-Export-Ref'), **preparation}
        profile = parse_server_profile(response.headers)
        if profile:
            result['server_profile'] = profile
        return result, body, response.headers.copy(), target_key
    except Exception as error:
        result = {'request_id': number, 'kind': kind, 'tier': tier, 'status': None,
                  'code': type(error).__name__, 'error_detail': type(error).__name__,
                  'ok': False, 'started': started, 'first': first,
                  'ended': time.perf_counter(), 'bytes': 0,
                  'expected_count': expected_count if 'export' in kind else None,
                  'server_ref': None, **preparation}
        return result, None, {}, target_key
    finally:
        if response is not None:
            response.close()
        if active:
            with LOCK:
                ACTIVE -= 1
        if result is not None:
            result['client_response_finalize_ms'] = (time.perf_counter() - result['ended']) * 1000


def validate_capture(capture):
    result, body, headers, target_key = capture
    result.update({'rows': None, 'sha256_match': None, 'content_length': None,
                   'header_byte_count': None, 'declared_sha256': None, 'record_count': None})
    if body is None:
        return result
    kind = result['kind']
    if result['status'] != 200:
        result.update({'ok': False, 'error_detail': 'HTTP response body omitted from evidence'})
        try:
            result['code'] = json.loads(body).get('error', {}).get('code')
        except (ValueError, UnicodeError):
            result['code'] = 'NON_JSON_ERROR'
        return result
    try:
        if 'export' in kind:
            declared_bytes = int(headers['Content-Length'])
            header_bytes = int(headers['X-Byte-Count'])
            declared_sha256 = headers['X-Content-SHA256']
            digest_match = (declared_bytes == len(body) == header_bytes and
                            hashlib.sha256(body).hexdigest() == declared_sha256)
            reader = csv.reader(io.StringIO(body.decode('utf-8')))
            fieldnames = next(reader, [])
            first_values = next((values for values in reader if values), None)
            rows = (1 + sum(1 for values in reader if values)) if first_values else 0
            record_count = int(headers['X-Record-Count'])
            result.update({'rows': rows, 'sha256_match': digest_match,
                           'content_length': declared_bytes, 'header_byte_count': header_bytes,
                           'declared_sha256': declared_sha256, 'record_count': record_count})
            expected = result['expected_count']
            if kind in ('clinical_export', 'detail_export'):
                valid = digest_match and record_count == expected and rows == expected
            else:
                first_row = dict(zip(fieldnames, first_values or []))
                valid = (digest_match and rows == 1 and record_count == expected and
                         first_row['rule_ref'] == 'SR-02' and
                         int(first_row['hit_count']) == expected and
                         int(first_row['alert_count']) == expected and
                         int(first_row['unhandled_count']) == expected)
        else:
            obj = json.loads(body)
            valid = obj.get('ok') is not False
            if target_key:
                valid = update_current_version(target_key, obj) and valid
            if kind == 'reception':
                valid = valid and bool(obj.get('encounter_ref'))
        result.update({'code': None, 'ok': bool(valid), 'error_detail': None})
    except Exception as error:
        result.update({'code': type(error).__name__, 'ok': False,
                       'error_detail': type(error).__name__})
    return result


def validator(pending, results, recorder, stop, errors):
    while True:
        capture = pending.get()
        try:
            if capture is stop:
                return
            try:
                result = validate_capture(capture)
                results.append(result)
                recorder.request(result)
            except Exception as error:
                if not errors:
                    errors.append(error)
        finally:
            pending.task_done()


class ValidationQueue(queue.Queue):
    def submit(self, capture):
        item = (capture, time.perf_counter())
        capture[0]['validation_queue_full'] = False
        try:
            self.put_nowait(item)
        except queue.Full:
            capture[0]['validation_queue_full'] = True
            self.put(item)

    def _put(self, item):
        # Set timing under the queue lock, before the validator can see the row.
        if isinstance(item, tuple):
            capture, queued_at = item
            capture[0]['validation_enqueue_wait_ms'] = (time.perf_counter() - queued_at) * 1000
            item = capture
        super()._put(item)


def worker(index, gate, end_at, base, cookies, actors, pending):
    rng = random.Random(20261024 + index)
    session = requests.Session()
    try:
        gate.wait(timeout=180)
        while time.perf_counter() < end_at:
            task_started = time.perf_counter()
            number, kind = next_task()
            task_selected = time.perf_counter()
            capture = run_one(session, base, cookies, actors, number, kind, rng)
            capture[0]['worker_id'] = index
            capture[0]['client_task_selection_ms'] = (task_selected - task_started) * 1000
            capture[0]['client_inline_validation_ms'] = 0.0
            if kind in ('guard', 'write'):
                validating = time.perf_counter()
                capture = (validate_capture(capture), None, {}, None)
                capture[0]['client_inline_validation_ms'] = (time.perf_counter() - validating) * 1000
            pending.submit(capture)
    finally:
        session.close()


def summarize(rows, samples, start, end, warmup):
    formal = [r for r in rows if start + warmup <= r['started'] < end]
    groups = {}
    for kind in sorted(set(MIX)):
        selected = [r for r in formal if r['kind'] == kind]
        times = [(r['ended'] - r['started']) * 1000 for r in selected]
        profiled = [r.get('server_profile', {}) for r in selected if r.get('server_profile')]
        profile_metrics = {}
        profile_scopes = sorted({row['scope'] for row in profiled if row.get('scope')})
        for metric in SERVER_TIMINGS.values():
            values = [row[metric] for row in profiled if metric in row]
            if values:
                profile_metrics[metric] = {'count': len(values), 'p50_ms': quantile(values, .5),
                                           'p95_ms': quantile(values, .95),
                                           'p99_ms': quantile(values, .99), 'max_ms': max(values)}
        groups[kind] = {'count': len(selected), 'failures': sum(not r['ok'] for r in selected),
                        'p50_ms': quantile(times, .5), 'p95_ms': quantile(times, .95),
                        'p99_ms': quantile(times, .99), 'max_ms': max(times, default=None),
                        'profiled_count': len(profiled), 'profile_scopes': profile_scopes,
                        'server_profile': profile_metrics}
        if 'export' in kind:
            groups[kind]['tiers'] = {}
            for tier in sorted({r.get('tier') for r in selected}):
                tier_rows = [r for r in selected if r.get('tier') == tier]
                tier_times = [(r['ended'] - r['started']) * 1000 for r in tier_rows]
                groups[kind]['tiers'][tier] = {
                    'count': len(tier_rows), 'failures': sum(not r['ok'] for r in tier_rows),
                    'p50_ms': quantile(tier_times, .5), 'p95_ms': quantile(tier_times, .95),
                    'p99_ms': quantile(tier_times, .99), 'max_ms': max(tier_times, default=None)}
    minutes = []
    for minute in range(math.ceil(max(0, end - start - warmup) / 60)):
        left = start + warmup + minute * 60
        right = min(left + 60, end)
        selected = [r for r in rows if left <= r['ended'] < right]
        counts = {'minute': minute + 1, 'completed': len(selected),
                  'success': sum(r['ok'] for r in selected), 'failures': sum(not r['ok'] for r in selected)}
        if right - left < 60:
            counts['duration_seconds'] = right - left
        minutes.append(counts)
    during = [s['in_flight'] for s in samples if warmup <= s['seconds'] < end - start]
    return {'kind': 'diagnostic', 'request_namespace': str(REQUEST_NAMESPACE),
            'planned_seconds': end - start, 'warmup_seconds': warmup,
            'formal_requests_started': len(formal), 'formal_failures': sum(not r['ok'] for r in formal),
            'groups': groups, 'minutes': minutes, 'in_flight_min': min(during, default=None),
            'in_flight_p50': quantile(during, .5), 'in_flight_p95': quantile(during, .95),
            'in_flight_max': max(during, default=None),
            'actual_mix': {kind: groups[kind]['count'] / len(formal) if formal else 0 for kind in groups}}


class RequestStatistics:
    """Keep exact numeric samples, never retain full request dictionaries."""

    def __init__(self, start, end, warmup):
        self.start, self.end, self.warmup = start, end, warmup
        self.left = start + warmup
        self.lock = threading.Lock()
        self.groups = {kind: {'times': array('d'), 'failures': 0, 'profiled': 0,
                             'scopes': set(), 'metrics': {}, 'tiers': {}}
                       for kind in sorted(set(MIX))}
        self.minutes = []
        for minute in range(math.ceil(max(0, end - self.left) / 60)):
            left, right = self.left + minute * 60, min(self.left + (minute + 1) * 60, end)
            row = {'minute': minute + 1, 'completed': 0, 'success': 0, 'failures': 0}
            if right - left < 60:
                row['duration_seconds'] = right - left
            self.minutes.append(row)
        self.edges = [self.left + i * 60 for i in range(len(self.minutes))] + [end]

    def append(self, row):
        with self.lock:
            if self.left <= row['ended'] < self.end:
                minute = self.minutes[bisect_right(self.edges, row['ended']) - 1]
                minute['completed'] += 1
                minute['success' if row['ok'] else 'failures'] += 1
            if not self.left <= row['started'] < self.end:
                return
            group = self.groups[row['kind']]
            elapsed = (row['ended'] - row['started']) * 1000
            group['times'].append(elapsed)
            group['failures'] += not row['ok']
            profile = row.get('server_profile')
            if profile:
                group['profiled'] += 1
                if profile.get('scope'):
                    group['scopes'].add(profile['scope'])
                for metric in SERVER_TIMINGS.values():
                    if metric in profile:
                        group['metrics'].setdefault(metric, array('d')).append(profile[metric])
            if 'export' in row['kind']:
                tier = group['tiers'].setdefault(row.get('tier'), {'times': array('d'), 'failures': 0})
                tier['times'].append(elapsed)
                tier['failures'] += not row['ok']

    @staticmethod
    def timings(values):
        return {'p50_ms': quantile(values, .5), 'p95_ms': quantile(values, .95),
                'p99_ms': quantile(values, .99), 'max_ms': max(values, default=None)}

    def summarize(self, samples):
        groups = {}
        for kind, group in self.groups.items():
            groups[kind] = {'count': len(group['times']), 'failures': group['failures'],
                            **self.timings(group['times']), 'profiled_count': group['profiled'],
                            'profile_scopes': sorted(group['scopes']),
                            'server_profile': {metric: {'count': len(values), **self.timings(values)}
                                               for metric, values in group['metrics'].items()}}
            if 'export' in kind:
                groups[kind]['tiers'] = {tier: {'count': len(value['times']),
                    'failures': value['failures'], **self.timings(value['times'])}
                    for tier, value in sorted(group['tiers'].items())}
        total = sum(group['count'] for group in groups.values())
        during = [row['in_flight'] for row in samples
                  if self.warmup <= row['seconds'] < self.end - self.start]
        return {'kind': 'diagnostic', 'request_namespace': str(REQUEST_NAMESPACE),
                'planned_seconds': self.end - self.start, 'warmup_seconds': self.warmup,
                'formal_requests_started': total,
                'formal_failures': sum(group['failures'] for group in groups.values()),
                'groups': groups, 'minutes': self.minutes,
                'in_flight_min': min(during, default=None), 'in_flight_p50': quantile(during, .5),
                'in_flight_p95': quantile(during, .95), 'in_flight_max': max(during, default=None),
                'actual_mix': {kind: group['count'] / total if total else 0 for kind, group in groups.items()}}


SERVER_TIMINGS = {'queue': 'queue_ms', 'app': 'app_ms', 'sql': 'sql_ms', 'commit': 'commit_ms',
                  'db-connect': 'db_connect_ms',
                  'export-snapshot': 'export_snapshot_ms', 'export-csv': 'export_csv_ms',
                  'export-audit-targets': 'export_audit_targets_ms'}


def parse_server_profile(headers):
    """Parse optional timing headers emitted by 05_正式负载服务.py --profile."""
    timing = headers.get('Server-Timing', '')
    result = {}
    for name, value in re.findall(r'(?:^|,)\s*([\w-]+);dur=([0-9]+(?:\.[0-9]+)?)', timing):
        if name in SERVER_TIMINGS:
            result[SERVER_TIMINGS[name]] = float(value)
    for header, key, converter in (
            ('X-T22-SQL-Count', 'sql_count', int),
            ('X-T22-DB-Connect-Count', 'db_connect_count', int),
            ('X-T22-DB-Connect-Error-Code', 'db_connect_error_code', int),
            ('X-T22-Server-PID', 'server_pid', int),
            ('X-T22-Process-CPU', 'process_cpu_seconds', float),
            ('X-T22-App-Start', 'app_start_monotonic', float),
            ('X-T22-App-End', 'app_end_monotonic', float),
            ('X-T22-App-Start-UTC', 'app_start_utc', str),
            ('X-T22-App-End-UTC', 'app_end_utc', str)):
        try:
            result[key] = converter(headers[header])
        except (KeyError, TypeError, ValueError):
            pass
    scope = headers.get('X-T22-Profile-Scope')
    if scope:
        result['scope'] = scope
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:18830')
    parser.add_argument('--target-base', action='append', default=[],
                        help='service endpoint; repeat to distribute workers across app ports')
    parser.add_argument('--tunnel-base', action='append', default=[],
                        help='legacy alias for --target-base')
    parser.add_argument('--private-accounts', type=Path, required=True)
    parser.add_argument('--output-prefix', type=Path, required=True)
    parser.add_argument('--concurrency', type=int, default=360)
    parser.add_argument('--shard-count', type=int, default=1)
    parser.add_argument('--shard-index', type=int, default=0)
    parser.add_argument('--common-clock', type=Path)
    parser.add_argument('--warmup-seconds', type=int, default=300)
    parser.add_argument('--formal-seconds', type=int, default=600)
    parser.add_argument('--gil-switch-interval-ms', type=float,
                        help='optional sender interpreter scheduling candidate; acceptance limits are unchanged')
    parser.add_argument('--capture-gc-pauses', action='store_true',
                        help='buffer GC events and save a timeline after the workload; does not change GC settings')
    parser.add_argument('--server-pid', type=int)
    parser.add_argument('--mysql-pid', type=int)
    parser.add_argument('--only-group', choices=['query', 'guard', 'reception', 'write',
                                                  'clinical_export', 'detail_export', 'summary_export'])
    args = parser.parse_args()
    try:
        request_id_base = configure_client_shard(args.shard_count, args.shard_index)
    except ValueError as error:
        parser.error(str(error))
    if args.shard_count > 1 and (args.common_clock is None or args.only_group):
        parser.error('Sharded runs require a common clock and the complete workload mix')
    if args.gil_switch_interval_ms is not None and (
            not math.isfinite(args.gil_switch_interval_ms) or args.gil_switch_interval_ms <= 0):
        parser.error('GIL switch interval must be finite and positive')
    if args.concurrency < 1 or args.output_prefix.with_suffix('.summary.json').exists():
        parser.error('Invalid concurrency or existing result; results are never overwritten')
    private_path = args.private_accounts.resolve()
    output_path = args.output_prefix.resolve()
    if REPO_ROOT == output_path or REPO_ROOT in output_path.parents or \
            REPO_ROOT == private_path or REPO_ROOT in private_path.parents:
        parser.error('Keep private credentials and raw load evidence outside the repository')
    private = json.loads(private_path.read_text(encoding='utf-8'))
    if not private.get('synthetic_only') or private['manifest']['seed'] != 20261024:
        parser.error('Expected only the deterministic synthetic fixture')
    if private['manifest'].get('suggestion_targets') != ADOPTION_TARGETS:
        parser.error('Suggestion fixture capacity mismatch; create fresh data with 01_seed_formal_data.py')
    actors = {a['username']: a for a in private['accounts']}
    global SUMMARY_HITS_BY_STORE
    SUMMARY_HITS_BY_STORE = {store['store_ref']: store['guard_hits'] for store in private['manifest']['stores']}
    if args.only_group:
        MIX[:] = [args.only_group]
    try:
        bases = resolve_target_bases(args)
    except ValueError as error:
        parser.error(str(error))
    common_clock = None
    if args.common_clock:
        clock_path = args.common_clock.resolve()
        if REPO_ROOT == clock_path or REPO_ROOT in clock_path.parents:
            parser.error('Keep shared run clocks outside the repository')
        try:
            common_clock = read_common_clock(clock_path, args.shard_count, args.concurrency,
                                             args.warmup_seconds, args.formal_seconds)
        except (OSError, ValueError) as error:
            parser.error(str(error))
    cookies = authentication(bases[0], private)
    if common_clock and time.perf_counter() >= common_clock['monotonic_start']:
        raise RuntimeError('Common start missed; the run must not silently shorten warmup')
    prefix = args.output_prefix
    private_path = args.private_accounts.resolve()
    output_path = prefix.resolve()
    if REPO_ROOT == output_path or REPO_ROOT in output_path.parents or \
            REPO_ROOT == private_path or REPO_ROOT in private_path.parents:
        parser.error('Keep private credentials and raw load evidence outside the repository')
    recorder = JsonlRunRecorder(prefix)
    total = args.warmup_seconds + args.formal_seconds
    if common_clock:
        start = common_clock['monotonic_start']
        started_at_utc = datetime.fromisoformat(common_clock['utc_start'].replace('Z', '+00:00')).astimezone(timezone.utc)
        while time.perf_counter() < start:
            time.sleep(min(.1, max(0, start - time.perf_counter())))
    else:
        started_at_utc = datetime.now(timezone.utc)
        start = time.perf_counter()
    recorder.set_clock(start, started_at_utc)
    end = start + total
    worker_offset = args.shard_index * args.concurrency
    gate = threading.Barrier(args.concurrency + 1)
    results = RequestStatistics(start, end, args.warmup_seconds)
    pending = ValidationQueue(maxsize=args.concurrency * 2)
    validation_stop = object()
    validation_errors = []
    validators = [threading.Thread(target=validator,
                                  args=(pending, results, recorder, validation_stop, validation_errors), daemon=True)
                  for _ in range(8)]
    for thread in validators:
        thread.start()
    watcher = threading.Thread(target=monitor, args=(start, started_at_utc, recorder, pending), daemon=True)
    watcher.start()
    resource_watcher = threading.Thread(target=monitor_resources,
                                        args=(start, started_at_utc, args.server_pid, args.mysql_pid, recorder),
                                        daemon=True)
    resource_watcher.start()
    try:
        with gil_switch_interval(args.gil_switch_interval_ms) as active_interval, \
                GCPauseRecorder(prefix, start, started_at_utc, args.capture_gc_pauses), \
                ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(worker, n + worker_offset, gate, end, bases[(n + worker_offset) % len(bases)], cookies, actors, pending)
                       for n in range(args.concurrency)]
            gate.wait(timeout=180)
            for future in futures:
                future.result()
    finally:
        pending.join()
        for _ in validators:
            pending.put(validation_stop)
        pending.join()
        for thread in validators:
            thread.join(timeout=5)
        STOP_SAMPLER.set()
        watcher.join(timeout=5)
        resource_watcher.join(timeout=5)
        recorder.close()
    if validation_errors:
        raise RuntimeError('Run response validation or recording failed') from validation_errors[0]
    summary = results.summarize(SAMPLES)
    actual_duration = time.perf_counter() - start
    exact_protocol = (args.concurrency == 360 and args.warmup_seconds == 300 and
                      args.formal_seconds == 600 and args.only_group is None and
                      actual_duration >= total)
    summary['kind'] = 'formal_window_candidate' if exact_protocol else 'diagnostic'
    summary['shard_count'] = args.shard_count
    summary['shard_index'] = args.shard_index
    summary['worker_offset'] = worker_offset
    summary['request_id_base'] = request_id_base
    summary['common_clock_sha256'] = common_clock['sha256'] if common_clock else None
    summary['common_start_monotonic'] = start
    summary['fixture_database'] = private['manifest']['database']
    summary['load_script_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    summary['gc_pause_capture'] = args.capture_gc_pauses
    summary['gil_switch_interval_ms'] = active_interval * 1000
    if args.only_group:
        summary['only_group'] = args.only_group
    summary.update({'concurrency': args.concurrency, 'sample_count': len(SAMPLES),
                    'formal_seconds': args.formal_seconds,
                    'actual_duration_seconds': actual_duration,
                    'run_started_at_utc': utc_at(started_at_utc, 0),
                    'formal_started_at_utc': utc_at(started_at_utc, args.warmup_seconds),
                    'formal_ended_at_utc': utc_at(started_at_utc, args.warmup_seconds + args.formal_seconds),
                    'run_finished_at_utc': utc_at(started_at_utc, actual_duration),
                    'timestamps_utc_note': 'Mapped from the run-start UTC wall clock plus monotonic elapsed time; latency uses perf_counter.',
                    'tunnel_count': len(bases),
                    'target_endpoint_count': len(bases),
                    'target_worker_assignments': target_worker_assignments(args.concurrency, len(bases), worker_offset),
                    'target_pool_sizes': {'guard': GUARD_TARGETS,
                                          'diagnosis_correction': DIAGNOSIS_TARGETS,
                                          'suggestion_adoption': ADOPTION_TARGETS},
                    'target_assignments': dict(TARGETS),
                    'resource_samples': len(RESOURCES), 'os': platform.platform(),
                    'validation_queue_peak': max((sample.get('validation_pending', 0) for sample in SAMPLES), default=0),
                    'python': platform.python_version(), 'requests': requests.__version__,
                    'psutil': psutil.__version__,
                    'resource_peaks': {key: max((row.get(key, 0) for row in RESOURCES), default=None)
                                       for key in ('host_cpu_percent', 'host_memory_percent',
                                                   'web_rss_bytes', 'mysql_rss_bytes', 'load_rss_bytes')}})
    prefix.with_suffix('.summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
