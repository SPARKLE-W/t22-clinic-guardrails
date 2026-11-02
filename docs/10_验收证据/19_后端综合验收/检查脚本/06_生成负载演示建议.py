"""Add clearly labelled synthetic suggestions to the isolated formal-load fixture."""
import os
import sys
import uuid
from pathlib import Path

CODE = Path(__file__).resolve().parents[4] / 'backend'
sys.path.insert(0, str(CODE))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vetapp.settings')

import django
django.setup()

from django.conf import settings
from clinic.models import Encounter, Suggestion

NAMESPACE = uuid.UUID('23863255-7eb6-4692-96e2-cb87d8dbf54c')


def ref(kind, store, number):
    return uuid.uuid5(NAMESPACE, f'20261024:{kind}:{store}:{number}')

if os.environ.get('T22_FORMAL_LOAD') != '1' or not settings.DATABASES['default']['NAME'].startswith('t22_perf_'):
    raise SystemExit('Explicit isolated formal-load database is required')
if Encounter.objects.count() != 20000:
    raise SystemExit('Expected exactly 20000 synthetic encounters; no changes made')

source_version = 'demo-formal-load-v1'
existing = list(Suggestion.objects.all())
if any(row.source_version != source_version or row.source.get('type') != 'demo' or
       row.source.get('version') != source_version for row in existing):
    raise SystemExit('Unexpected non-synthetic suggestion exists; no changes made')
existing_ids = {str(row.pk) for row in existing}
rows = []
for shop in range(5):
    for number in range(2, 4000, 3):
        suggestion_id = ref('suggestion', shop, number)
        if str(suggestion_id) not in existing_ids:
            rows.append(Suggestion(id=suggestion_id,
                                   encounter_id=ref('encounter', shop, number),
                                   source_version=source_version, sample_key=f'synthetic-{number}',
                                   source={'type': 'demo', 'version': source_version,
                                           'label': '合成压测演示建议，不是真实模型建议'},
                                   content={'medication': {'drug': '合成建议药品', 'amount': '1', 'unit': '片'}}))
for start in range(0, len(rows), 500):
    Suggestion.objects.bulk_create(rows[start:start + 500], batch_size=500)
print(f'synthetic_suggestion_targets=6665 added={len(rows)} total={Suggestion.objects.count()}')
