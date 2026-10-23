"""Add clearly labelled synthetic suggestions to the isolated formal-load fixture."""
import os
import sys
import uuid
from pathlib import Path

CODE = Path(__file__).resolve().parents[3] / '05_系统代码'
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
if Suggestion.objects.exists():
    raise SystemExit('Suggestions already exist; no changes made')

source_version = 'demo-formal-load-v1'
rows = []
for shop in range(5):
    for number in range(1, 4000, 10):
        rows.append(Suggestion(id=ref('suggestion', shop, number),
                               encounter_id=ref('encounter', shop, number),
                               source_version=source_version, sample_key=f'synthetic-{number}',
                               source={'type': 'demo', 'version': source_version,
                                       'label': '合成压测演示建议，不是真实模型建议'},
                               content={'medication': {'drug': '合成建议药品', 'amount': '1', 'unit': '片'}}))
for start in range(0, len(rows), 500):
    Suggestion.objects.bulk_create(rows[start:start + 500], batch_size=500)
print(f'synthetic_suggestions={len(rows)}')
