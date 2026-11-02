"""Same workload in one/multiple load processes; common wall window, raw timings."""
import argparse
import hashlib
import json
import math
import multiprocessing as mp
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent.parent / '原始结果/性能定位'


def p95(values):
    return sorted(values)[math.ceil(0.95 * len(values)) - 1] if values else None


def observe_database(stop, ready, samples, errors):
    """One dedicated read-only status connection; no query bodies or credentials."""
    import MySQLdb
    database = None
    try:
        config = json.loads((Path.home() / '.cache/t22-runtime/private/connections.json').read_text(encoding='utf-8-sig'))['mysql']
        database = MySQLdb.connect(host=config['host'], port=int(config['port']), user=config['user'], passwd=config['password'], db=config['database'])
        database.autocommit(True)
        while not stop.is_set():
            with database.cursor() as cursor:
                cursor.execute("SHOW GLOBAL STATUS WHERE Variable_name IN ('Threads_connected','Threads_running','Max_used_connections','Connection_errors_max_connections')")
                values = {name: int(value) for name, value in cursor.fetchall()}
            samples.append({'perf_counter': time.perf_counter(), **values})
            ready.set()
            stop.wait(0.5)
    except Exception as exc:
        errors.append(type(exc).__name__)
        ready.set()
    finally:
        if database is not None:
            database.close()


def child(indices, ready, gate, start, seconds, results, port):
    import load_probe as workload
    workload.BASE = f'http://127.0.0.1:{port}'
    from django.db import connections
    with ThreadPoolExecutor(max_workers=8) as pool:
        workers = list(pool.map(workload.login_worker, indices))
    connections.close_all()
    local_gate = threading.Barrier(len(workers) + 1)

    def run(worker):
        rows = []
        local_gate.wait(timeout=120)
        while time.perf_counter() < start.value + seconds:
            before = time.perf_counter()
            try:
                response = workload.perform(worker)
                content = response.content
                ended = time.perf_counter()
                valid_started = time.perf_counter()
                ok, code = workload.validate(worker, response)
                validation_ms = (time.perf_counter() - valid_started) * 1000
                timing = {name: float(value) for name, value in re.findall(r'(app|sql|commit|queue);dur=([0-9.]+)', response.headers.get('Server-Timing', ''))}
                row = {'http': response.status_code, 'bytes': len(content), 'ok': ok, 'code': code,
                       'validation_ms': validation_ms, 'timing': timing,
                       'sql_count': int(response.headers.get('X-PRE-SQL-Count', 0)),
                       'server_pid': int(response.headers.get('X-PRE-Server-PID', 0)),
                       'server_cpu_cumulative_seconds': float(response.headers.get('X-PRE-Process-CPU', 0))}
                if 'X-PRE-App-Start' in response.headers:
                    row['before_app_ms'] = (float(response.headers['X-PRE-App-Start']) - before) * 1000
                    row['after_app_ms'] = (ended - float(response.headers['X-PRE-App-End'])) * 1000
            except (workload.requests.RequestException, ValueError, KeyError, workload.csv.Error) as exc:
                ended = time.perf_counter()
                row = {'http': None, 'bytes': 0, 'ok': False, 'code': type(exc).__name__, 'timing': {}}
            row.update(ms=(ended - before) * 1000, started_s=before - start.value,
                       ended_s=ended - start.value, worker=worker['index'], category=worker['category'],
                       load_pid=os.getpid())
            if 'app' in row['timing']:
                row['outside_app_ms'] = row['ms'] - row['timing']['app']
                row['other_than_app_and_dispatch_ms'] = row['outside_app_ms'] - row['timing'].get('queue', 0)
            rows.append(row)
        worker['session'].close()
        return rows

    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        futures = [pool.submit(run, worker) for worker in workers]
        ready.put({'pid': os.getpid(), 'workers': len(workers)})
        gate.wait(timeout=120)
        cpu_started = time.process_time()
        local_gate.wait(timeout=120)
        rows = [row for future in futures for row in future.result()]
    results.put({'pid': os.getpid(), 'rows': rows, 'cpu_seconds': time.process_time() - cpu_started,
                 'ended_s': time.perf_counter() - start.value})


def main(args):
    OUTPUT.mkdir(exist_ok=True)
    prefix = OUTPUT / args.tag
    if prefix.with_name(prefix.name + '_summary.json').exists():
        raise RuntimeError('Refuse to overwrite an existing result tag.')
    context = mp.get_context('spawn')
    ready, results = context.Queue(), context.Queue()
    gate, start = context.Event(), context.Value('d', 0)
    processes = [context.Process(target=child, args=(list(range(part, args.concurrency, args.processes)),
                  ready, gate, start, args.seconds, results, args.ports[part % len(args.ports)])) for part in range(args.processes)]
    setup_started = time.perf_counter()
    for process in processes:
        process.start()
    prepared = [ready.get(timeout=180) for _ in processes]
    setup_seconds = time.perf_counter() - setup_started
    db_stop, db_ready = threading.Event(), threading.Event()
    db_samples, db_errors = [], []
    db_thread = threading.Thread(target=observe_database, args=(db_stop, db_ready, db_samples, db_errors))
    db_thread.start()
    db_ready.wait(timeout=5)
    start.value = time.perf_counter()
    started_unix = time.time()
    gate.set()
    batches = [results.get(timeout=120) for _ in processes]
    db_stop.set()
    db_thread.join(timeout=10)
    for process in processes:
        process.join(timeout=15)
        if process.exitcode != 0:
            raise RuntimeError('Load child did not exit cleanly.')
    rows = [row for batch in batches for row in batch['rows']]
    prefix.with_name(prefix.name + '_samples.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    # Last complete validation/session close, not parent IPC transfer time.
    wall = max(batch['ended_s'] for batch in batches)
    groups = {}
    for category in ['reception', 'query', 'guarded_diagnosis', 'medication_write', 'export']:
        selected = [row for row in rows if row['category'] == category]
        groups[category] = {'samples': len(selected), 'failures': sum(not r['ok'] for r in selected),
                            'p95_all_ms': p95([r['ms'] for r in selected]),
                            'p95_app_ms': p95([r['timing']['app'] for r in selected if 'app' in r['timing']]),
                            'p95_sql_ms': p95([r['timing']['sql'] for r in selected if 'sql' in r['timing']]),
                            'p95_commit_ms': p95([r['timing']['commit'] for r in selected if 'commit' in r['timing']]),
                            'p95_dispatch_ms': p95([r['timing']['queue'] for r in selected if 'queue' in r['timing']]),
                            'p95_outside_app_ms': p95([r['outside_app_ms'] for r in selected if 'outside_app_ms' in r]),
                            'sql_count_min_max': [min((r['sql_count'] for r in selected if 'sql_count' in r), default=None), max((r['sql_count'] for r in selected if 'sql_count' in r), default=None)],
                            'max_bytes': max((r['bytes'] for r in selected), default=0),
                            'p95_before_app_ms': p95([r['before_app_ms'] for r in selected if 'before_app_ms' in r]),
                            'p95_after_app_ms': p95([r['after_app_ms'] for r in selected if 'after_app_ms' in r])}
    summary = {'kind': 'profiled_short_probe_not_AC06', 'concurrency': args.concurrency,
               'services_sha256': hashlib.sha256((Path(__file__).parent / 'probe/services.py').read_bytes()).hexdigest().upper(),
               'load_processes': args.processes, 'server_processes': args.backend_processes or len(args.ports), 'server_threads_total': args.server_threads,
               'ports': args.ports, 'entry_kind': args.entry_kind,
               'database_status_samples': db_samples, 'database_monitor_errors': db_errors,
               'database_status_scope': 'one extra observer connection; Max_used_connections cumulative since MySQL start, other fields sampled during this run',
               'requested_seconds': args.seconds, 'started_unix': started_unix, 'setup_seconds_excluded': setup_seconds,
               'wall_including_drain_seconds': wall, 'samples': len(rows), 'failures': sum(not r['ok'] for r in rows),
               'successful_per_minute': sum(r['ok'] for r in rows) / wall * 60,
               'groups': groups, 'load_process_cpu_seconds': {str(batch['pid']): batch['cpu_seconds'] for batch in batches},
               'prepared_processes': prepared, 'server_pids': sorted({r.get('server_pid', 0) for r in rows})}
    prefix.with_name(prefix.name + '_samples.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    prefix.with_name(prefix.name + '_summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in summary.items() if key not in ['database_status_samples', 'prepared_processes']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--processes', type=int, default=1)
    parser.add_argument('--concurrency', type=int, default=360)
    parser.add_argument('--seconds', type=int, default=10)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--ports', type=int, nargs='+', default=[18722])
    parser.add_argument('--server-threads', type=int, default=32)
    parser.add_argument('--backend-processes', type=int)
    parser.add_argument('--entry-kind', choices=['direct_port_shards_not_unified_proxy', 'unified_proxy'], default='direct_port_shards_not_unified_proxy')
    args = parser.parse_args()
    if not 1 <= args.processes <= args.concurrency or not 1 <= args.seconds <= 60:
        parser.error('Bounded process/concurrency/duration required.')
    main(args)
