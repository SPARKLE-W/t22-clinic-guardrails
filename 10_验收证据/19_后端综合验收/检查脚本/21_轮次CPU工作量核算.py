"""Summarize process CPU consumption against requests in one load run.

The ratio is an operational estimate, not per-request CPU attribution: resource
samples may include startup, shutdown, and unrelated server work.
"""
import argparse
import csv
import json
from pathlib import Path


def summarize(requests_path, resources_path, load_seconds=None):
    request_count = 0
    failures = 0
    with Path(requests_path).open(encoding='utf-8') as source:
        for line in source:
            if not line.strip():
                continue
            request = json.loads(line)
            request_count += 1
            failures += not bool(request.get('ok'))

    with Path(resources_path).open(encoding='utf-8-sig', newline='') as source:
        samples = list(csv.DictReader(source))
    if request_count == 0 or len(samples) < 2:
        raise ValueError('Need at least one request and two resource samples')

    first, last = samples[0], samples[-1]
    elapsed_seconds = float(last['elapsed_s']) - float(first['elapsed_s'])
    if elapsed_seconds <= 0:
        raise ValueError('Resource sample elapsed time must increase')

    process_cpu = {}
    for name in ('web_cpu_seconds', 'mysql_cpu_seconds'):
        delta = float(last[name]) - float(first[name])
        if delta < 0:
            raise ValueError(f'{name} counter decreased')
        process_cpu[name] = round(delta, 3)
    combined_cpu = sum(process_cpu.values())

    result = {
        'request_count': request_count,
        'request_failures': failures,
        'resource_sample_count': len(samples),
        'resource_observation_seconds': round(elapsed_seconds, 3),
        'process_cpu_seconds': process_cpu,
        'combined_process_cpu_seconds': round(combined_cpu, 3),
        'average_observed_process_cores': round(combined_cpu / elapsed_seconds, 3),
        'combined_process_cpu_seconds_per_request': round(combined_cpu / request_count, 6),
        'interpretation': (
            'Aggregate ratio only; it is not request-level attribution and may '
            'include work outside the load interval.'
        ),
    }
    if load_seconds is not None:
        if load_seconds <= 0:
            raise ValueError('--load-seconds must be positive')
        result['planned_load_seconds'] = load_seconds
        result['requests_started_per_planned_load_second'] = round(request_count / load_seconds, 3)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('requests_jsonl')
    parser.add_argument('server_resources_csv')
    parser.add_argument('--load-seconds', type=float,
                        help='planned request-generation window, excluding drain time')
    args = parser.parse_args()
    print(json.dumps(summarize(args.requests_jsonl, args.server_resources_csv, args.load_seconds),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
