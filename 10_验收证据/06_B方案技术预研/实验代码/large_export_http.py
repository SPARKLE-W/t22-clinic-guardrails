"""One complete HTTP export of all 4000 load_0 doctor's authorized records."""
import csv
import io
import json
import time
import load_probe as workload
from django.db import connections
from probe.models import Audit, AuditTarget, Record, Revision, Medication

worker = workload.login_worker(1)  # query category authenticates load_0_doctor
session = worker['session']
records = list(Record.objects.filter(encounter__store__code='load_0',
               encounter__doctor__username='load_0_doctor').order_by('id'))
ids = [str(record.id) for record in records]
expected = set(ids)
expected_versions = {str(record.id): record.version for record in records}
expected_revisions = Revision.objects.filter(record_id__in=ids).count()
expected_medications = Medication.objects.filter(record_id__in=ids).count()
actor_id = records[0].encounter.doctor_id
before_audit = Audit.objects.filter(actor_id=actor_id, action='export.clinical').order_by('-id').values_list('id', flat=True).first() or 0
connections.close_all()
started = time.perf_counter()
response = session.post(workload.BASE + '/probe/exports', json={'record_ids': ids, 'purpose': 'PRE all-own-4000 HTTP export'}, timeout=60)
body = response.content
elapsed_ms = (time.perf_counter() - started) * 1000
response.raise_for_status()
rows = list(csv.DictReader(io.StringIO(body.decode('utf-8'))))
current = [row for row in rows if row['kind'] == 'record']
audits = list(Audit.objects.filter(id__gt=before_audit, actor_id=actor_id, action='export.clinical', result='success'))
assert len(records) == len(current) == len({row['record_id'] for row in current}) == 4000
assert {row['record_id'] for row in current} == expected
assert all(int(row['version']) == expected_versions[row['record_id']] for row in current)
assert sum(row['kind'] == 'revision' for row in rows) == expected_revisions
assert sum(row['kind'] == 'medication' for row in rows) == expected_medications
assert len(audits) == 1
audit_ids = list(map(str, AuditTarget.objects.filter(audit=audits[0]).values_list('record_id', flat=True)))
assert len(audit_ids) == 4000 and set(audit_ids) == expected
result = {'kind': 'single_large_HTTP_response_not_P95_or_AC06', 'records': len(current), 'csv_rows': len(rows),
          'revisions': expected_revisions, 'medications': expected_medications, 'utf8_bytes': len(body),
          'elapsed_full_response_ms': elapsed_ms, 'audit_target_count': len(audit_ids),
          'complete_scope_versions_history_and_audit': True, 'http_status': response.status_code}
output = workload.ROOT / '原始结果/性能定位/large_4000_HTTP_batch.json'
if output.exists():
    raise RuntimeError('Refuse to overwrite existing large HTTP result.')
output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(result, ensure_ascii=False), flush=True)
session.close()
connections.close_all()
