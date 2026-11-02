"""Align profiled load requests with same-host resource samples by UTC minute.

The output is diagnostic correlation data only. It does not prove causation.
"""
import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean


KINDS = ('query', 'guard', 'reception', 'write', 'clinical_export',
         'detail_export', 'summary_export')
PROFILE_METRICS = ('queue_ms', 'app_ms', 'sql_ms', 'commit_ms')
PROCESS_NAMES = {'python': 'python', 'mysqld': 'mysql', 'caddy': 'caddy'}
DISK_METRICS = {
    'read_iops': 'disk_read_iops',
    'write_iops': 'disk_write_iops',
    'average_read_latency_ms': 'disk_read_latency_ms',
    'average_write_latency_ms': 'disk_write_latency_ms',
    'queue_length': 'disk_queue_length',
}


def parse_utc(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def percentile(values, q=0.95):
    if not values:
        return ''
    return round(sorted(values)[max(0, math.ceil(q * len(values)) - 1)], 3)


def _read_jsonl(path):
    with Path(path).open(encoding='utf-8-sig') as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def _minute_key(value):
    return value.replace(second=0, microsecond=0)


def _resource_bucket(buckets, key):
    bucket = buckets.setdefault(key, {})
    bucket.setdefault('resource_sample_count', 0)
    bucket.setdefault('resource_error_count', 0)
    bucket.setdefault('host_cpu_percent', [])
    bucket.setdefault('available_memory_bytes', [])
    bucket.setdefault('process_cpu_delta_seconds', defaultdict(float))
    bucket.setdefault('disk_sample_count', 0)
    bucket.setdefault('disk_metrics', defaultdict(list))
    return bucket


def _request_bucket(buckets, key):
    bucket = buckets.setdefault(key, {})
    bucket.setdefault('requests', defaultdict(lambda: {
        'count': 0, 'failures': 0, 'e2e_ms': [], 'profiled_count': 0,
        'queue_ms': [], 'app_ms': [], 'sql_ms': [], 'commit_ms': []}))
    return bucket


def build_aligned_rows(requests_path, resources_path):
    buckets = {}
    request_total = 0
    unmatched_requests = 0
    for row in _read_jsonl(requests_path):
        request_total += 1
        profile = row.get('server_profile') or {}
        started_at = parse_utc(profile.get('app_start_utc'))
        if started_at is None:
            unmatched_requests += 1
            continue
        request = _request_bucket(buckets, _minute_key(started_at))['requests'][row.get('kind', 'unknown')]
        request['count'] += 1
        request['failures'] += not bool(row.get('ok'))
        try:
            request['e2e_ms'].append((float(row['ended']) - float(row['started'])) * 1000)
        except (KeyError, TypeError, ValueError):
            pass
        request['profiled_count'] += 1
        for metric in PROFILE_METRICS:
            try:
                request[metric].append(float(profile[metric]))
            except (KeyError, TypeError, ValueError):
                continue

    previous_processes = {}
    for sample in _read_jsonl(resources_path):
        sampled_at = parse_utc(sample.get('timestamp_utc'))
        if sampled_at is None:
            continue
        bucket = _resource_bucket(buckets, _minute_key(sampled_at))
        bucket['resource_sample_count'] += 1
        if sample.get('sampling_error'):
            bucket['resource_error_count'] += 1
            previous_processes = {}
            continue
        for field, target in (('host_cpu_percent', 'host_cpu_percent'),
                              ('available_memory_bytes', 'available_memory_bytes')):
            try:
                bucket[target].append(float(sample[field]))
            except (KeyError, TypeError, ValueError):
                pass

        current_processes = {}
        for process in sample.get('processes') or []:
            try:
                pid = int(process['pid'])
                name = PROCESS_NAMES.get(str(process['name']).casefold())
                cpu = float(process['cpu_cumulative_seconds'])
            except (KeyError, TypeError, ValueError):
                continue
            if name is None:
                continue
            current_processes[pid] = (name, cpu)
            previous = previous_processes.get(pid)
            if previous and previous[0] == name and cpu >= previous[1]:
                bucket['process_cpu_delta_seconds'][name] += cpu - previous[1]
        previous_processes = current_processes

        disks = sample.get('physical_disks') or []
        physical = [disk for disk in disks if str(disk.get('instance', '')).casefold() != '_total']
        selected_disks = physical or disks
        if selected_disks:
            bucket['disk_sample_count'] += 1
        for disk in selected_disks:
            for source, target in DISK_METRICS.items():
                try:
                    bucket['disk_metrics'][target].append(float(disk[source]))
                except (KeyError, TypeError, ValueError):
                    pass

    columns = ['utc_minute', 'request_count', 'request_failures', 'unmatched_requests',
               'resource_sample_count', 'resource_error_count', 'host_cpu_mean_percent',
               'host_cpu_max_percent', 'available_memory_min_bytes', 'python_cpu_delta_seconds',
               'mysql_cpu_delta_seconds', 'caddy_cpu_delta_seconds', 'disk_sample_count']
    for metric in DISK_METRICS.values():
        columns.append(metric + '_mean')
    for kind in KINDS:
        columns.extend([f'{kind}_count', f'{kind}_failures', f'{kind}_profiled_count',
                        f'{kind}_e2e_p95_ms', f'{kind}_queue_p95_ms', f'{kind}_app_p95_ms',
                        f'{kind}_sql_p95_ms', f'{kind}_commit_p95_ms'])

    results = []
    for key in sorted(buckets):
        source = buckets[key]
        requests = source.get('requests', {})
        row = {
            'utc_minute': key.isoformat(timespec='minutes').replace('+00:00', 'Z'),
            'request_count': sum(value['count'] for value in requests.values()),
            'request_failures': sum(value['failures'] for value in requests.values()),
            'unmatched_requests': unmatched_requests,
            'resource_sample_count': source.get('resource_sample_count', 0),
            'resource_error_count': source.get('resource_error_count', 0),
            'host_cpu_mean_percent': round(fmean(source.get('host_cpu_percent', [])), 3) if source.get('host_cpu_percent') else '',
            'host_cpu_max_percent': max(source.get('host_cpu_percent', []), default=''),
            'available_memory_min_bytes': min(source.get('available_memory_bytes', []), default=''),
            'python_cpu_delta_seconds': round(source.get('process_cpu_delta_seconds', {}).get('python', 0.0), 3),
            'mysql_cpu_delta_seconds': round(source.get('process_cpu_delta_seconds', {}).get('mysql', 0.0), 3),
            'caddy_cpu_delta_seconds': round(source.get('process_cpu_delta_seconds', {}).get('caddy', 0.0), 3),
            'disk_sample_count': source.get('disk_sample_count', 0),
        }
        disk_metrics = source.get('disk_metrics', {})
        for metric in DISK_METRICS.values():
            values = disk_metrics.get(metric, [])
            row[metric + '_mean'] = round(fmean(values), 3) if values else ''
        for kind in KINDS:
            values = requests.get(kind, {})
            row[f'{kind}_count'] = values.get('count', 0)
            row[f'{kind}_failures'] = values.get('failures', 0)
            row[f'{kind}_profiled_count'] = values.get('profiled_count', 0)
            row[f'{kind}_e2e_p95_ms'] = percentile(values.get('e2e_ms', []))
            for metric in PROFILE_METRICS:
                row[f'{kind}_{metric.removesuffix('_ms')}_p95_ms'] = percentile(values.get(metric, []))
        results.append(row)
    return columns, results, {'request_count': request_total, 'unmatched_requests': unmatched_requests,
                              'aligned_request_count': request_total - unmatched_requests,
                              'resource_minute_count': sum(bool(b.get('resource_sample_count')) for b in buckets.values())}


def write_csv(path, columns, rows):
    target = Path(path)
    if not target.parent.exists():
        raise FileNotFoundError(f'Output directory does not exist: {target.parent}')
    with target.open('x', encoding='utf-8-sig', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=columns, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('requests_jsonl')
    parser.add_argument('server_resources_jsonl')
    parser.add_argument('output_csv')
    args = parser.parse_args()
    columns, rows, summary = build_aligned_rows(args.requests_jsonl, args.server_resources_jsonl)
    write_csv(args.output_csv, columns, rows)
    print(json.dumps({**summary, 'output_csv': str(Path(args.output_csv).resolve()),
                      'bucket': 'UTC minute of profiled app start / resource sample',
                      'interpretation': 'Aligned diagnostic summaries only; no causal attribution.'},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
