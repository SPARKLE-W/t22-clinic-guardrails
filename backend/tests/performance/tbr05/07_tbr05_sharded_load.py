"""Run independent client shards with one clock, preserving original evidence."""
import argparse
import importlib.util
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


def run_clients(commands, prefix, deadline, sample=None):
    processes, streams, states = [], [], []
    try:
        for index, command in enumerate(commands):
            stream = Path(str(prefix) + f'-shard-{index}.stdout.log').open('x', encoding='utf-8')
            streams.append(stream)
            process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            processes.append(process)
        while True:
            codes = [p.poll() for p in processes]
            if any(code not in (None, 0) for code in codes):
                raise RuntimeError('A client shard failed; all siblings will be stopped')
            if all(code == 0 for code in codes):
                break
            if time.perf_counter() >= deadline:
                raise TimeoutError('Client run deadline exceeded')
            if sample:
                sample(processes)
            time.sleep(.2)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            states.append({'pid': process.pid, 'returncode': process.returncode})
        for stream in streams:
            stream.close()
        with prefix.with_suffix('.children.json').open('x', encoding='utf-8') as output:
            json.dump(states, output, indent=2)
    return states


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample_client_processes(psutil, root_pids, cpu_last):
    """Include venv launcher descendants; CPU is each lifetime's last observed total."""
    candidates, errors, observed = [], [], {}
    for pid in root_pids:
        try:
            process = psutil.Process(pid)
            candidates.append(process)
            candidates.extend(process.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
            errors.append({'pid': pid, 'error': type(error).__name__})
    for process in candidates:
        try:
            birth = process.create_time()
            identity = (process.pid, birth)
            if identity in observed:
                continue
            rss = process.memory_info().rss
            cpu = process.cpu_times()
            cpu_last[identity] = cpu.user + cpu.system
            observed[identity] = {'pid': process.pid, 'create_time': birth,
                                  'rss_bytes': rss, 'cpu_seconds': cpu_last[identity]}
        except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
            errors.append({'pid': process.pid, 'error': type(error).__name__})
    return {'load_rss_bytes': sum(p['rss_bytes'] for p in observed.values()),
            'load_cpu_seconds': sum(cpu_last.values()), 'load_process_count': len(observed),
            'load_processes': sorted(observed.values(), key=lambda p: (p['pid'], p['create_time'])),
            'load_resource_complete': not errors, 'load_resource_errors': errors,
            'load_resource_scope': 'controller-and-descendants-v2'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-accounts', type=Path, required=True)
    parser.add_argument('--output-prefix', type=Path, required=True)
    parser.add_argument('--target-base', action='append', required=True)
    parser.add_argument('--concurrency', type=int, default=360)
    parser.add_argument('--processes', type=int, default=4)
    parser.add_argument('--warmup-seconds', type=int, default=300)
    parser.add_argument('--formal-seconds', type=int, default=600)
    parser.add_argument('--gil-switch-interval-ms', type=float)
    parser.add_argument('--capture-gc-pauses', action='store_true')
    args = parser.parse_args()
    if args.processes < 1 or args.concurrency < args.processes or args.concurrency % args.processes:
        parser.error('Concurrency must divide evenly across positive process count')
    driver_path = Path(__file__).with_name('03_tbr05_load.py')
    driver = module_at('sharded_driver', driver_path)
    evidence = module_at('sharded_evidence', Path(__file__).with_name('shard_evidence.py'))
    prefix = args.output_prefix.resolve()
    if driver.REPO_ROOT == prefix or driver.REPO_ROOT in prefix.parents or prefix.suffix:
        parser.error('Use a suffix-free output prefix outside the repository')
    prefix.parent.mkdir(parents=True, exist_ok=True)
    if list(prefix.parent.glob(prefix.name + '*')):
        parser.error('Run evidence already exists; never overwrite')
    local = args.concurrency // args.processes
    driver.configure_client_shard(args.processes, args.processes - 1)
    origin = time.perf_counter() + 45
    utc = datetime.now(timezone.utc) + timedelta(seconds=45)
    clock = {'clock_version': 1, 'shard_count': args.processes, 'concurrency': args.concurrency,
             'warmup_seconds': args.warmup_seconds, 'formal_seconds': args.formal_seconds,
             'monotonic_start': origin, 'utc_start': utc.isoformat()}
    clock_path = prefix.with_suffix('.clock.json')
    with clock_path.open('x', encoding='utf-8') as stream:
        json.dump(clock, stream, indent=2)
    parts = [prefix.parent / f'{prefix.name}-shard-{i}' for i in range(args.processes)]
    commands = []
    for index, part in enumerate(parts):
        command = [sys.executable, str(driver_path), '--private-accounts', str(args.private_accounts),
                   '--output-prefix', str(part), '--concurrency', str(local),
                   '--shard-count', str(args.processes), '--shard-index', str(index),
                   '--common-clock', str(clock_path), '--warmup-seconds', str(args.warmup_seconds),
                   '--formal-seconds', str(args.formal_seconds)]
        for base in args.target_base:
            command.extend(['--target-base', base])
        if args.gil_switch_interval_ms is not None:
            command.extend(['--gil-switch-interval-ms', str(args.gil_switch_interval_ms)])
        if args.capture_gc_pauses:
            command.append('--capture-gc-pauses')
        commands.append(command)
    resource_rows, cpu_last = [], {}
    parent = driver.psutil.Process(os.getpid())
    last_sample = float('-inf')
    with prefix.with_suffix('.resources.jsonl').open('x', encoding='utf-8') as resources:
        def sample(processes):
            nonlocal last_sample
            now = time.perf_counter()
            if now - last_sample < 1:
                return
            last_sample = now
            process_resources = sample_client_processes(
                driver.psutil, [parent.pid] + [p.pid for p in processes], cpu_last)
            row = {'seconds': now - origin, 'timestamp_utc': driver.utc_at(utc, now - origin),
                   'host_cpu_percent': driver.psutil.cpu_percent(interval=None),
                   'host_memory_percent': driver.psutil.virtual_memory().percent,
                   **process_resources}
            resource_rows.append(row)
            resources.write(json.dumps(row) + '\n')
            resources.flush()
        run_clients(commands, prefix, origin + args.warmup_seconds + args.formal_seconds + 300, sample)
    _, reports = evidence.verify_shard_reports(parts, clock_path)
    if args.capture_gc_pauses:
        events = []
        for index, part in enumerate(parts):
            for event in json.loads(part.with_suffix('.gc.json').read_text(encoding='utf-8')):
                events.append({**event, 'shard_index': index})
        events.sort(key=lambda event: (event['t'], event['shard_index']))
        with prefix.with_suffix('.gc.json').open('x', encoding='utf-8') as stream:
            json.dump(events, stream)
    shards = [[json.loads(line) for line in part.with_suffix('.inflight.jsonl').open(encoding='utf-8')]
              for part in parts]
    samples = evidence.merge_inflight_samples(shards, local, args.warmup_seconds, args.formal_seconds)
    statistics = driver.RequestStatistics(0, args.warmup_seconds + args.formal_seconds, args.warmup_seconds)
    with prefix.with_suffix('.requests.jsonl').open('x', encoding='utf-8') as stream:
        for row in evidence.iter_request_rows(parts, reports):
            statistics.append(row)
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    with prefix.with_suffix('.inflight.jsonl').open('x', encoding='utf-8') as stream:
        for row in samples:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    summary = {**reports[0], **statistics.summarize(samples)}
    elapsed = time.perf_counter() - origin
    summary.update(controller_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   concurrency=args.concurrency, shard_count=args.processes,
                   driver_mode='independent_process_shards', actual_duration_seconds=elapsed,
                   sample_count=len(samples), resource_samples=len(resource_rows),
                   run_finished_at_utc=driver.utc_at(utc, elapsed),
                   validation_queue_peak=max((r['validation_pending'] for r in samples), default=0),
                   target_assignments=dict(sum((Counter(r['target_assignments']) for r in reports), Counter())),
                   target_worker_assignments=[sum(v) for v in zip(*(r['target_worker_assignments'] for r in reports))],
                   resource_peaks={k: max((r[k] for r in resource_rows), default=None) for k in
                                   ('host_cpu_percent', 'host_memory_percent', 'load_rss_bytes')})
    summary['resource_sampling_version'] = 2
    summary['incomplete_resource_samples'] = sum(not r['load_resource_complete'] for r in resource_rows)
    for key in ('shard_index', 'worker_offset', 'request_id_base'):
        summary.pop(key, None)
    summary['kind'] = 'formal_window_candidate' if (args.concurrency == 360 and args.warmup_seconds == 300
                                                   and args.formal_seconds == 600) else 'diagnostic'
    with prefix.with_suffix('.summary.json').open('x', encoding='utf-8') as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
