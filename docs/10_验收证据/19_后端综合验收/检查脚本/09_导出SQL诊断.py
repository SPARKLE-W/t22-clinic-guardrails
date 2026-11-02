"""Count SQL statements for one isolated 4000-record export."""
import collections
import re
import os
import sys
import time
import uuid
from pathlib import Path

CODE = Path(__file__).resolve().parents[4] / 'backend'
sys.path.insert(0, str(CODE))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vetapp.settings')
import django
django.setup()

from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from governance.models import Account
from clinic.exports import clinical

if os.environ.get('T22_FORMAL_LOAD') != '1' or not settings.DATABASES['default']['NAME'].startswith('t22_perf_'):
    raise SystemExit('Isolated formal-load database required')
namespace = uuid.UUID('23863255-7eb6-4692-96e2-cb87d8dbf54c')
refs = [str(uuid.uuid5(namespace, f'20261024:record:0:{n}')) for n in range(4000)]
actor = Account.objects.get(username='load_1_doctor_1')
start = time.perf_counter()
with CaptureQueriesContext(connection) as queries:
    body, _, count = clinical(actor.pk, {'scope': {'record_refs': refs}, 'purpose': '合成SQL诊断'})
summary = collections.Counter(row['sql'].split(' ')[0] for row in queries)
print({'records': count, 'bytes': len(body), 'queries': len(queries),
       'verbs': dict(summary), 'seconds': round(time.perf_counter() - start, 3),
       'sql_seconds': round(sum(float(row['time']) for row in queries), 3),
       'query_timings': [(row['sql'].split(' ')[0], row['time'],
                         (re.search(r'FROM `([^`]+)`', row['sql']) or [None, None])[1], len(row['sql']))
                         for row in queries]})
