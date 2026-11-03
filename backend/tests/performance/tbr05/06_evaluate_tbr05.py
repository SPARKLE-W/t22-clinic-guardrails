"""Fail-closed evaluation of raw evidence against frozen TBR-05 v1.0."""
import argparse
import json
import math
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
WARMUP_SECONDS = 300
FORMAL_SECONDS = 600
CONCURRENCY = 360
MINUTE_SUCCESS = 800
P95_LIMIT_MS = 280
KINDS = ('query', 'guard', 'reception', 'write', 'clinical_export',
         'detail_export', 'summary_export')
EXPORT_KINDS = ('clinical_export', 'detail_export', 'summary_export')
EXPORT_TIERS = ('1-20', '50-200', '1000-4000')


def percentile(values, fraction):
    if not values:
        return None
    return sorted(values)[math.ceil(fraction * len(values)) - 1]


def read_jsonl(path):
    rows = []
    with Path(path).open(encoding='utf-8-sig') as stream:
        for line_number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise ValueError(f'Invalid JSONL at {path}:{line_number}') from error
    return rows


def _timing_report(rows):
    elapsed = [(row['ended'] - row['started']) * 1000 for row in rows]
    return {'count': len(rows), 'failures': sum(not row.get('ok') for row in rows),
            'p50_ms': percentile(elapsed, .50), 'p95_ms': percentile(elapsed, .95),
            'p99_ms': percentile(elapsed, .99), 'max_ms': max(elapsed, default=None)}


def evaluate(summary, request_rows, inflight_rows):
    failures = []
    invalid_rows = [row for row in request_rows if any(
        type(row.get(field)) not in (int, float) or not math.isfinite(row[field])
        for field in ('started', 'ended')) or row.get('ended', 0) < row.get('started', 0)]
    if invalid_rows:
        return {'passed': False, 'failures': ['invalid_request_timing'],
                'invalid_request_count': len(invalid_rows)}
    expected_duration = WARMUP_SECONDS + FORMAL_SECONDS
    protocol_shape = (summary.get('concurrency') == CONCURRENCY and
                      summary.get('warmup_seconds') == WARMUP_SECONDS and
                      summary.get('formal_seconds') == FORMAL_SECONDS and
                      summary.get('only_group') is None and
                      summary.get('actual_duration_seconds', 0) >= expected_duration)
    if not protocol_shape:
        failures.append('protocol_parameters_or_duration_mismatch')

    formal_end = expected_duration
    formal_requests = [row for row in request_rows
                       if WARMUP_SECONDS <= row.get('started', -1) < formal_end]
    unknown_kinds = sorted({row.get('kind') for row in formal_requests} - set(KINDS))
    if unknown_kinds:
        failures.append('unknown_request_classes:' + ','.join(str(kind) for kind in unknown_kinds))
    by_kind = {kind: [row for row in formal_requests if row.get('kind') == kind]
               for kind in KINDS}
    missing_kinds = [kind for kind, rows in by_kind.items() if not rows]
    if missing_kinds:
        failures.append('missing_request_classes:' + ','.join(missing_kinds))

    class_report = {}
    for kind, rows in by_kind.items():
        class_report[kind] = _timing_report(rows)
        metric = class_report[kind]['p95_ms']
        if metric is None or metric > P95_LIMIT_MS:
            failures.append(f'p95_over_limit_or_missing:{kind}')
        if class_report[kind]['failures']:
            failures.append(f'request_failures:{kind}')

    export_report = {}
    for kind in EXPORT_KINDS:
        rows = by_kind[kind]
        tiers = {}
        for tier in EXPORT_TIERS if kind != 'summary_export' else ('summary',):
            selected = [row for row in rows if row.get('tier') == tier]
            tiers[tier] = _timing_report(selected)
            if not selected:
                failures.append(f'missing_export_tier:{kind}:{tier}')
        export_report[kind] = tiers

    for row in formal_requests:
        if row.get('kind') in EXPORT_KINDS:
            kind = row['kind']
            expected_rows = 1 if kind == 'summary_export' else row.get('expected_count')
            expected_count = row.get('expected_count')
            actual_bytes = row.get('bytes')
            content_length = row.get('content_length')
            header_bytes = row.get('header_byte_count')
            declared_sha256 = row.get('declared_sha256')
            if (row.get('status') != 200 or row.get('ok') is not True or
                    row.get('sha256_match') is not True or
                    not isinstance(expected_count, int) or expected_count < 0 or
                    row.get('record_count') != expected_count or
                    row.get('rows') != expected_rows or
                    not isinstance(actual_bytes, int) or actual_bytes < 0 or
                    content_length != actual_bytes or header_bytes != actual_bytes or
                    not isinstance(declared_sha256, str) or
                    re.fullmatch(r'[0-9a-f]{64}', declared_sha256) is None):
                failures.append(f'export_incomplete:{row.get("request_id", "unknown")}:{kind}')
            if kind != 'summary_export':
                bounds = {'1-20': (1, 20), '50-200': (50, 200), '1000-4000': (1000, 4000)}
                lower, upper = bounds.get(row.get('tier'), (1, 0))
                if type(expected_count) is not int or not lower <= expected_count <= upper:
                    failures.append(f'export_scope_outside_tier:{row.get("request_id", "unknown")}')

    # Count completions in each measured minute, even if the request began during warmup.
    minutes = []
    for minute in range(10):
        left = WARMUP_SECONDS + minute * 60
        right = left + 60
        completed = [row for row in request_rows if left <= row.get('ended', -1) < right]
        success = sum(row.get('ok') is True for row in completed)
        failed = sum(row.get('ok') is not True for row in completed)
        minutes.append({'minute': minute + 1, 'completed': len(completed),
                        'success': success, 'failures': failed})
        if success < MINUTE_SUCCESS:
            failures.append(f'minute_throughput_below_800:{minute + 1}')

    formal_failures = [row for row in request_rows
                       if row.get('ok') is not True and
                       (WARMUP_SECONDS <= row.get('started', -1) < formal_end or
                        WARMUP_SECONDS <= row.get('ended', -1) < formal_end)]
    if formal_failures:
        failures.append('authorized_business_failures_in_formal_window')

    samples = sorted((row for row in inflight_rows
                      if WARMUP_SECONDS <= row.get('seconds', -1) < formal_end),
                     key=lambda row: row['seconds'])
    gaps = [right['seconds'] - left['seconds'] for left, right in zip(samples, samples[1:])]
    covered = bool(samples) and samples[0]['seconds'] <= WARMUP_SECONDS + .5 and \
        samples[-1]['seconds'] >= formal_end - .5 and max(gaps, default=float('inf')) <= .5
    observed = [row.get('in_flight') for row in samples]
    exact_inflight = covered and all(value == CONCURRENCY for value in observed)
    if not exact_inflight:
        failures.append('360_continuous_inflight_not_proven')

    mix_counts = {kind: len(rows) for kind, rows in by_kind.items()}
    mix_total = len(formal_requests)
    mix_percent = {kind: round(count * 100 / mix_total, 3) if mix_total else 0
                   for kind, count in mix_counts.items()}

    return {'passed': not failures, 'failures': failures,
            'scope': 'Automated request and sampled-inflight gates only; not a complete acceptance decision.',
            'manual_evidence_required': ['source_and_configuration_identity', 'fixture_counts',
                                         'fixed_workload_mix_and_size_distribution',
                                         'client_and_server_resources', 'server_error_logs',
                                         'full_scope_export_and_peak_memory',
                                         'request_interval_concurrency_reconstruction'],
            'protocol': {'expected_concurrency': CONCURRENCY,
                         'warmup_seconds': WARMUP_SECONDS,
                         'formal_seconds': FORMAL_SECONDS,
                         'minimum_success_per_minute': MINUTE_SUCCESS,
                         'maximum_complete_response_p95_ms': P95_LIMIT_MS,
                         'formal_requests_started': len(formal_requests),
                         'observed_request_mix_percent': mix_percent},
            'classes': class_report, 'exports_by_scope_and_size': export_report,
            'minutes': minutes,
            'inflight': {'sample_count': len(samples), 'coverage_complete': covered,
                         'minimum': min(observed, default=None),
                         'median': percentile(observed, .5),
                         'maximum': max(observed, default=None),
                         'maximum_sample_gap_seconds': max(gaps, default=None),
                         'exact_360_proven': exact_inflight},
            'formal_failure_count': len(formal_failures)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--summary', type=Path, required=True)
    parser.add_argument('--requests', type=Path, required=True)
    parser.add_argument('--inflight', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    summary = json.loads(args.summary.read_text(encoding='utf-8'))
    report = evaluate(summary, read_jsonl(args.requests), read_jsonl(args.inflight))
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = args.output.resolve()
        if REPO_ROOT == output or REPO_ROOT in output.parents:
            parser.error('Acceptance reports belong outside the repository')
        with output.open('x', encoding='utf-8') as stream:
            stream.write(encoded + '\n')
    print(encoded)
    raise SystemExit(0 if report['passed'] else 2)


if __name__ == '__main__':
    main()
