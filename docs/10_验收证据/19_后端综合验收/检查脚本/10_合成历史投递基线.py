"""Close synthetic historical alert transport before measuring new requests."""
import os
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[4] / 'backend'
sys.path.insert(0, str(CODE))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vetapp.settings')
import django
django.setup()

from django.conf import settings
from django.db import transaction
from clinic.models import AlertOutbox, DeliveryReceipt

if os.environ.get('T22_FORMAL_LOAD') != '1' or not settings.DATABASES['default']['NAME'].startswith('t22_perf_'):
    raise SystemExit('Isolated formal-load database required')
if AlertOutbox.objects.count() != 12000:
    raise SystemExit('Expected exactly 12000 synthetic historical alerts')
with transaction.atomic():
    pending = list(AlertOutbox.objects.filter(state='pending').values_list('pk', flat=True))
    for start in range(0, len(pending), 500):
        DeliveryReceipt.objects.bulk_create(
            [DeliveryReceipt(event_id=key) for key in pending[start:start + 500]], batch_size=500)
    AlertOutbox.objects.filter(state='pending').update(state='delivered')
print({'synthetic_historical_receipts': len(pending),
       'delivered_total': AlertOutbox.objects.filter(state='delivered').count(),
       'pending_total': AlertOutbox.objects.filter(state='pending').count()})
