"""Independently recompute one completed course window; no acceptance by summary alone."""
import hashlib
import json
import math
import sys
from pathlib import Path

prefix = Path(sys.argv[1])
summary = json.loads(prefix.with_suffix('.summary.json').read_text(encoding='utf-8-sig'))
rows = [json.loads(line) for line in prefix.with_suffix('.requests.jsonl').open(encoding='utf-8-sig')]
assert summary['protocol_version'] == 'course-v1.1.3'
assert summary['kind'] == 'course_360_active_sessions'
assert (summary['warmup_seconds'], summary['formal_seconds'], summary['target_offset']) == (60, 600, 0)
assert summary['distinct_login_sessions'] == 360
formal = [r for r in rows if 60 <= r['started'] < 660]
assert len(formal) == summary['formal_requests_started']
assert {r['worker'] for r in formal} == set(range(360))
assert all(r['ok'] and r['status'] == 200 and r['ended'] >= r['started'] for r in rows)
groups = {}
for kind in ('query', 'guard', 'reception', 'write', 'clinical_export', 'detail_export', 'summary_export'):
    selected = [r for r in formal if r['kind'] == kind]
    values = sorted((r['ended'] - r['started']) * 1000 for r in selected)
    assert len(values) >= 100, kind
    p95 = values[math.ceil(.95 * len(values)) - 1]
    assert abs(p95 - summary['groups'][kind]['p95_ms']) < 1e-6
    assert p95 <= 280, (kind, p95)
    groups[kind] = {'count': len(values), 'p95_ms': p95}
minutes = []
for minute in range(10):
    selected = [r for r in rows if 60 + minute * 60 <= r['ended'] < 120 + minute * 60]
    assert len(selected) >= 800
    formal_completed = [r for r in formal if 60 + minute * 60 <= r['ended'] < 120 + minute * 60]
    assert len(formal_completed) == summary['minutes'][minute]['success']
    assert len(formal_completed) >= 800
    minutes.append(len(selected))
exports = [r for r in rows if 'export' in r['kind']]
assert all(r['sha256_match'] and r['bytes'] == r['content_length'] for r in exports)
assert len({r['server_ref'] for r in exports}) == len(exports)
for r in exports:
    if r['kind'] != 'summary_export':
        assert r['rows'] == r['record_count'] == r['expected_count']
    else:
        assert r['rows'] == 1 and r['record_count'] >= 0
result = {'scope': 'course active-session client gates only; exact server audit separately required',
          'passed': True, 'formal_requests': len(formal), 'groups': groups,
          'minute_successes': minutes, 'exports_including_warmup': len(exports),
          'files': {suffix: hashlib.sha256(prefix.with_suffix('.' + suffix).read_bytes()).hexdigest()
                    for suffix in ('summary.json', 'requests.jsonl', 'inflight.jsonl', 'resources.jsonl')}}
prefix.with_suffix('.independent-verification.json').write_text(json.dumps(result, indent=2))
prefix.with_suffix('.export-index.json').write_text(json.dumps([
    {k: r[k] for k in ('kind', 'server_ref', 'rows', 'record_count', 'content_length', 'worker')}
    for r in exports]))
print(json.dumps(result))
