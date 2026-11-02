"""Capture only nonsecret response/connection diagnostics from the lab."""
import json
import os
from pathlib import Path
import requests
os.environ.setdefault('DJANGO_SETTINGS_MODULE','settings')
import django
django.setup()
from probe.models import Record
from probe.fixtures import LAB_PASSWORD

session=requests.Session()
session.trust_env=False
base='http://127.0.0.1:18722'
first=session.get(base+'/probe/login',timeout=10)
session.post(base+'/probe/login',json={'username':'load_0_doctor','password':LAB_PASSWORD},headers={'X-CSRFToken':first.json()['csrf']},timeout=10).raise_for_status()
session.headers['X-CSRFToken']=session.cookies['csrftoken']
record=Record.objects.filter(encounter__store__code='load_0').first()
responses={'json':session.get(base+'/probe/encounters/'+str(record.encounter_id),timeout=10),
           'csv':session.post(base+'/probe/exports',json={'record_ids':[str(record.id)],'purpose':'PRE header diagnostic'},timeout=10)}
result={label:{'status':r.status_code,'body_bytes':len(r.content),'headers':{k:v for k,v in r.headers.items() if k.lower() in ['connection','content-length','transfer-encoding']}} for label,r in responses.items()}
session.close()
import sys
tag=sys.argv[1]
path=Path(__file__).resolve().parent.parent/'原始结果'/f'{tag}_response_headers.json'
path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
