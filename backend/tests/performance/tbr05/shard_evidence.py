"""Evidence checks for client shards sharing one monotonic clock.

Request intervals are preserved verbatim. Sample aggregation is an asynchronous
sum of the most recent actual shard samples, not proof of interval concurrency.
"""
import hashlib
import json
import math
from pathlib import Path


def verify_shard_reports(prefixes, clock_path):
    raw = Path(clock_path).read_bytes()
    clock = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    count = clock['shard_count']
    if len(prefixes) != count or clock['concurrency'] % count:
        raise ValueError('Incomplete shard set')
    workers = clock['concurrency'] // count
    reports = []
    for index, prefix in enumerate(prefixes):
        report = json.loads(Path(prefix).with_suffix('.summary.json').read_text(encoding='utf-8'))
        expected = {'shard_index': index, 'shard_count': count, 'concurrency': workers,
                    'worker_offset': index * workers, 'request_id_base': index * 10 ** 12,
                    'common_clock_sha256': digest, 'common_start_monotonic': clock['monotonic_start'],
                    'warmup_seconds': clock['warmup_seconds'], 'formal_seconds': clock['formal_seconds']}
        if any(report.get(k) != v for k, v in expected.items()):
            raise ValueError('Shard identity or clock mismatch')
        duration = report.get('actual_duration_seconds')
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration < clock['warmup_seconds'] + clock['formal_seconds']:
            raise ValueError('Shard window incomplete')
        if reports and any(report.get(k) != reports[0].get(k) for k in
                           ('fixture_database', 'load_script_sha256', 'gil_switch_interval_ms', 'target_endpoint_count')):
            raise ValueError('Shard source, fixture or configuration mismatch')
        for key in ('fixture_database', 'load_script_sha256', 'target_endpoint_count'):
            if not report.get(key):
                raise ValueError('Shard identity evidence missing')
        reports.append(report)
    return clock, reports


def iter_request_rows(prefixes, reports):
    if not prefixes or len(prefixes) != len(reports):
        raise ValueError('Incomplete shard request set')
    seen = set()
    for prefix, report in zip(prefixes, reports):
        offset, workers = report['worker_offset'], report['concurrency']
        base = report['request_id_base']
        observed = set()
        with Path(prefix).with_suffix('.requests.jsonl').open(encoding='utf-8') as stream:
            for line in stream:
                row = json.loads(line)
                worker, request = row.get('worker_id'), row.get('request_id')
                if type(worker) is not int or not offset <= worker < offset + workers:
                    raise ValueError('Request worker outside owning shard')
                if type(request) is not int or not base <= request < base + 10 ** 12 or request in seen:
                    raise ValueError('Duplicate or foreign request identifier')
                if any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k]) for k in ('started', 'ended')) or row['ended'] < row['started']:
                    raise ValueError('Invalid original request interval')
                seen.add(request)
                observed.add(worker)
                yield row
        if observed != set(range(offset, offset + workers)):
            raise ValueError('Missing worker evidence')


def merge_request_rows(prefixes, reports):
    return list(iter_request_rows(prefixes, reports))


def merge_inflight_samples(shards, workers_per_shard, warmup, formal):
    if not shards or workers_per_shard < 1:
        raise ValueError('Incomplete shard sample set')
    end = warmup + formal
    for samples in shards:
        if any(type(r.get('seconds')) not in (int, float) or not math.isfinite(r['seconds']) or r['seconds'] < 0 for r in samples):
            raise ValueError('Invalid shard sample time')
        selected = sorted((r for r in samples if warmup <= r['seconds'] < end), key=lambda r: r['seconds'])
        if not selected or selected[0]['seconds'] > warmup + .5 or selected[-1]['seconds'] < end - .5 or any(b['seconds'] - a['seconds'] > .5 for a, b in zip(selected, selected[1:])):
            raise ValueError('Shard sampled evidence incomplete')
        if any(type(r.get('in_flight')) is not int or not 0 <= r['in_flight'] <= workers_per_shard or type(r.get('validation_pending')) is not int or r['validation_pending'] < 0 for r in samples):
            raise ValueError('Invalid shard sample')
    events = sorted(((r['seconds'], index, r) for index, samples in enumerate(shards) for r in samples), key=lambda item: (item[0], item[1]))
    latest, merged = {}, []
    for at, index, row in events:
        latest[index] = row
        if len(latest) == len(shards):
            merged.append({'seconds': at, 'timestamp_utc': row.get('timestamp_utc'),
                           'in_flight': sum(r['in_flight'] for r in latest.values()),
                           'validation_pending': sum(r['validation_pending'] for r in latest.values()),
                           'shard_sample_seconds': [latest[i]['seconds'] for i in range(len(shards))],
                           'sample_source': 'sum_of_latest_actual_shard_samples'})
    return merged
