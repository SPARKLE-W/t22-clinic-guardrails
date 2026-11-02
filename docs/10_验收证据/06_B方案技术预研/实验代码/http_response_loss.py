"""Real HTTP transport loss after backend commit, using only a one-shot lab proxy."""
import json
import os
import socket
import threading
import uuid
from pathlib import Path
import requests
os.environ.setdefault('DJANGO_SETTINGS_MODULE','settings')
import django
django.setup()
from probe.fixtures import create_case,LAB_PASSWORD
from probe.models import Operation,Medication,Audit

case=create_case('http-loss')
base='http://127.0.0.1:18722'
session=requests.Session()
session.trust_env=False
initial=session.get(base+'/probe/login',timeout=10)
session.post(base+'/probe/login',json={'username':case['doctor'].username,'password':LAB_PASSWORD},headers={'X-CSRFToken':initial.json()['csrf']},timeout=10).raise_for_status()
session.headers['X-CSRFToken']=session.cookies['csrftoken']
listener=socket.socket()
listener.bind(('127.0.0.1',0))
listener.listen(1)
listener.settimeout(10)
port=listener.getsockname()[1]
received_response=threading.Event()
errors=[]
def relay_then_drop():
    try:
        with listener.accept()[0] as client, socket.create_connection(('127.0.0.1',18722),timeout=10) as upstream:
            client.settimeout(10)
            data=b''
            while b'\r\n\r\n' not in data: data+=client.recv(65536)
            header,body=data.split(b'\r\n\r\n',1)
            length=next(int(line.split(b':',1)[1]) for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:'))
            while len(body)<length: body+=client.recv(65536)
            upstream.sendall(header+b'\r\n\r\n'+body)
            # Wait for a real response byte: the application has already left
            # its business/audit transaction. Forward zero bytes to the caller.
            if upstream.recv(1): received_response.set()
    except Exception as exc:
        errors.append(type(exc).__name__)
    finally:
        listener.close()
worker=threading.Thread(target=relay_then_drop)
worker.start()
key=uuid.uuid4().hex
payload={'operation_key':key,'expected_version':1,'drug':'PRE transport test','amount':'1','unit':'u'}
path=f'/probe/encounters/{case["encounter"].id}/append'
client_failure=None
try:
    session.post(f'http://127.0.0.1:{port}'+path,json=payload,timeout=15)
except requests.RequestException as exc:
    client_failure=type(exc).__name__
worker.join(timeout=15)
assert received_response.is_set() and not errors
assert client_failure is not None
operation=Operation.objects.get(actor=case['doctor'],operation_key=key)
assert operation.status==200
retry=session.post(base+path,json=payload,timeout=10)
assert retry.status_code==200 and retry.json()==operation.outcome
assert Medication.objects.filter(record=case['record']).count()==1
assert Audit.objects.filter(actor=case['doctor'],action='medication.record').count()==1
result={'fault':'backend_response_discarded_by_one_shot_TCP_proxy','backend_response_observed':True,
        'client_exception_class':client_failure,'persisted_operation_status':operation.status,
        'retry_reused_original_result':True,'medication_count':1,'write_audit_count':1,
        'scope':'real HTTP response loss, not database connection close and not process crash'}
out=Path(__file__).resolve().parent.parent/'原始结果'/'http_commit_response_loss.json'
out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
session.close()
print(json.dumps(result,ensure_ascii=False))
