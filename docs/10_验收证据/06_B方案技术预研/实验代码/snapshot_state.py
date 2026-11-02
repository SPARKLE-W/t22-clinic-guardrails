"""Hash all PRE tables before/after a graceful MySQL restart; emit no row bodies."""
import hashlib
import json
import os
import sys
from pathlib import Path
os.environ.setdefault('DJANGO_SETTINGS_MODULE','settings')
import django
django.setup()
from django.apps import apps
from django.db import connection

tables={}
for model in apps.get_app_config('probe').get_models():
    digest=hashlib.sha256()
    count=0
    for row in model.objects.order_by(model._meta.pk.name).values().iterator(chunk_size=1000):
        digest.update((json.dumps(row,ensure_ascii=False,sort_keys=True,default=str)+'\n').encode('utf-8'))
        count+=1
    tables[model._meta.db_table]={'rows':count,'content_sha256':digest.hexdigest().upper()}
with connection.cursor() as cursor:
    cursor.execute('SELECT @@version,@@transaction_isolation,@@innodb_flush_log_at_trx_commit')
    version,isolation,flush=cursor.fetchone()
result={'scope':'all_probe_tables_no_auth_credentials','mysql_version':version,'isolation':isolation,'innodb_flush_log_at_trx_commit':flush,'tables':tables}
output=Path(__file__).resolve().parent.parent/'原始结果'
tag=sys.argv[1]
(output/f'{tag}_db_state.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
if tag=='after_restart':
    before=json.loads((output/'before_restart_db_state.json').read_text(encoding='utf-8'))
    result['all_rows_and_hashes_preserved']=tables==before['tables']
    (output/'db_restart_comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    assert result['all_rows_and_hashes_preserved']
print(json.dumps({'tag':tag,'tables':len(tables),'rows':sum(t['rows'] for t in tables.values()),'all_rows_and_hashes_preserved':result.get('all_rows_and_hashes_preserved')},ensure_ascii=False))
