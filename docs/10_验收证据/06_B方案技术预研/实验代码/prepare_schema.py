"""Repair only this empty, failed first-bootstrap schema; never drop user data."""
import os
import re
os.environ.setdefault('DJANGO_SETTINGS_MODULE','settings')
import django
django.setup()
from django.db import connection

with connection.cursor() as c:
    c.execute('SELECT DATABASE()')
    assert c.fetchone()[0] == 't22_probe', 'wrong database'
    c.execute('SHOW TABLES')
    tables = [r[0] for r in c.fetchall() if r[0].startswith('probe_')]
    # A concurrent, prematurely started MQ fixture setup left only these
    # synthetic Store/PolicyHead rows before auth_user existed. Prove there
    # are no encounter/clinical/audit/operation rows before this one-off reset.
    c.execute('SELECT code FROM probe_store')
    codes = [r[0] for r in c.fetchall()]
    assert all(re.fullmatch(r'mq-[a-f0-9]{32}_[a-f0-9]{10}', code) for code in codes)
    for table in tables:
        assert table.replace('_','').isalnum()
        c.execute(f'SELECT COUNT(*) FROM `{table}`')
        count = c.fetchone()[0]
        if table not in ['probe_store','probe_policyhead']:
            assert count == 0, 'Nonempty clinical/operation table: refuse reset'
        else:
            assert count == len(codes), 'Unexpected bootstrap fixture rows'
    c.execute('SET FOREIGN_KEY_CHECKS=0')
    try:
        for table in tables:
            c.execute(f'DROP TABLE `{table}`')
    finally:
        c.execute('SET FOREIGN_KEY_CHECKS=1')
print(f'Recovered failed bootstrap: {len(codes)} proven synthetic Store/PolicyHead pairs, no clinical/audit/operation data; no other database changed.')
