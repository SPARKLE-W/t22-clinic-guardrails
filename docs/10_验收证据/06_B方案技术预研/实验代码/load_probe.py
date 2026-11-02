"""Short backend HTTP probe, not formal AC-06 nor a browser/UI benchmark."""
import argparse
import csv
import io
import json
import math
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import requests

os.environ.setdefault('DJANGO_SETTINGS_MODULE','settings')
import django
django.setup()
from django.contrib.auth.models import User
from django.db import connection
from probe.fixtures import LAB_PASSWORD
from probe.models import Store,Profile,PolicyHead,Encounter,Record,Revision,Audit,Operation

ROOT = Path(__file__).resolve().parent.parent
BASE = 'http://127.0.0.1:18722'
CATEGORIES = ['reception','query','guarded_diagnosis','medication_write','export']


def p95(values):
    return sorted(values)[max(0,math.ceil(len(values)*0.95)-1)] if values else None


def seed():
    """5 load stores; 6000 distinct synthetic pet refs, 20000 formal records."""
    if Store.objects.filter(code='load_0').exists():
        if Record.objects.filter(encounter__store__code__startswith='load_').count()!=20000:
            raise RuntimeError('Partial load fixture; do not overwrite or hide it.')
        return
    from django.contrib.auth.hashers import make_password
    encoded = make_password(LAB_PASSWORD)
    for i in range(5):
        store=Store.objects.create(code=f'load_{i}')
        PolicyHead.objects.create(store=store)
        users={}
        for role in ['doctor','frontdesk','manager']:
            user=User.objects.create(username=f'load_{i}_{role}',password=encoded)
            Profile.objects.create(user=user,store=store,role=role)
            users[role]=user
        encounters=[Encounter(store=store,doctor=users['doctor'],receptionist=users['frontdesk'],pet_id=f'PRE-PET-{i}-{n%1200}',complaint='PRE synthetic complaint') for n in range(4000)]
        Encounter.objects.bulk_create(encounters,batch_size=500)
        records=[Record(encounter=e,diagnosis='PRE 合成诊断 "quote",\n版本1',examination='PRE 合成检查') for e in encounters]
        Record.objects.bulk_create(records,batch_size=500)
        Revision.objects.bulk_create([Revision(record=r,version=1,content={'diagnosis':r.diagnosis,'examination':r.examination,'adopted_text':''},actor=users['doctor']) for r in records],batch_size=500)


def dataset_info():
    encounters=Encounter.objects.filter(store__code__startswith='load_')
    return {'load_stores':Store.objects.filter(code__startswith='load_').count(),
            'distinct_synthetic_pet_refs':encounters.values('pet_id').distinct().count(),
            'formal_records':Record.objects.filter(encounter__store__code__startswith='load_').count(),
            'encounters_including_new_receptions':encounters.count(),
            'other_MQ_fixture_stores_excluded':Store.objects.exclude(code__startswith='load_').count()}


def login_worker(index):
    category=CATEGORIES[index%5]
    # Spread each category across all five stores rather than assigning it
    # to one store by the same modulus as the category.
    store_index=(index//5)%5
    role='frontdesk' if category=='reception' else 'doctor'
    session=requests.Session()
    session.trust_env=False
    response=session.get(BASE+'/probe/login',timeout=15)
    response.raise_for_status()
    csrf=response.json()['csrf']
    response=session.post(BASE+'/probe/login',json={'username':f'load_{store_index}_{role}','password':LAB_PASSWORD},headers={'X-CSRFToken':csrf},timeout=30)
    response.raise_for_status()
    session.headers['X-CSRFToken']=session.cookies['csrftoken']
    # One separate record per worker; avoid accidental version conflicts
    # in this authorized-success load. Contended cases have separate tests.
    record=list(Record.objects.filter(encounter__store__code=f'load_{store_index}').order_by('id')[index:index+1])[0]
    doctor=User.objects.get(username=f'load_{store_index}_doctor')
    result={'index':index,'category':category,'store_index':store_index,'session':session,
            'record_id':str(record.id),'encounter_id':str(record.encounter_id),
            'version':record.version,'doctor_id':doctor.id}
    # This worker's initialization may run in its own thread; close its ORM
    # connection so setup doesn't consume concurrency database slots.
    from django.db import connections
    connections.close_all()
    return result


def perform(worker):
    category=worker['category']
    session=worker['session']
    key=uuid.uuid4().hex
    if category=='query':
        return session.get(BASE+'/probe/encounters/'+worker['encounter_id'],timeout=30)
    if category=='export':
        return session.post(BASE+'/probe/exports',json={'record_ids':[worker['record_id']],'purpose':'PRE short load'},timeout=30)
    if category=='reception':
        return session.post(BASE+'/probe/encounters',json={'operation_key':key,'pet_id':f'PRE-PET-{worker["store_index"]}-{worker["index"]%1200}','species':'dog','complaint':'PRE load','doctor_id':worker['doctor_id']},timeout=30)
    data={'operation_key':key,'expected_version':worker['version']}
    if category=='guarded_diagnosis':
        mode='diagnosis'
        data.update(diagnosis='PRE guarded diagnosis',reason='PRE load correction')
    else:
        mode='append'
        data.update(drug='PRE drug',amount='1',unit='u')
    return session.post(BASE+f'/probe/encounters/{worker["encounter_id"]}/{mode}',json=data,timeout=30)


def validate(worker, response):
    if response.status_code!=200: return False,f'HTTP_{response.status_code}'
    if worker['category']=='export':
        rows=list(csv.DictReader(io.StringIO(response.content.decode('utf-8'))))
        good=any(r['kind']=='record' and r['record_id']==worker['record_id'] for r in rows)
        return good,'OK' if good else 'CSV_INCOMPLETE'
    result=response.json()
    if result.get('code') not in ['SAVED','OK']: return False,'UNEXPECTED_CODE'
    if worker['category'] in ['guarded_diagnosis','medication_write']:
        worker['version']=result['version']
    return True,'OK'


def phase(concurrency, seconds, tag):
    setup_start=time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(32,concurrency)) as setup_pool:
        workers=list(setup_pool.map(login_worker,range(concurrency)))
    setup_seconds=time.perf_counter()-setup_start
    barrier=threading.Barrier(concurrency+1)
    clock={}
    def run(worker):
        samples=[]
        barrier.wait(timeout=60)
        try:
            while time.perf_counter()<clock['deadline']:
                started=time.perf_counter()
                try:
                    response=perform(worker)
                    body=response.content  # materialized in full before timing ends.
                    ended=time.perf_counter()
                    ok,code=validate(worker,response)
                    sample={'started_s':started-clock['start'],'ended_s':ended-clock['start'],
                            'ms':(ended-started)*1000,'http':response.status_code,'bytes':len(body),'ok':ok,'code':code}
                except (requests.RequestException,ValueError,KeyError,csv.Error) as exc:
                    ended=time.perf_counter()
                    sample={'started_s':started-clock['start'],'ended_s':ended-clock['start'],
                            'ms':(ended-started)*1000,'http':None,'bytes':0,'ok':False,'code':type(exc).__name__,'error_detail':str(exc)[:600]}
                sample.update(worker=worker['index'],category=worker['category'])
                samples.append(sample)
        finally:
            worker['session'].close()
        return samples
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures=[pool.submit(run,w) for w in workers]
        clock['started_unix']=time.time()
        clock['start']=time.perf_counter()
        clock['deadline']=clock['start']+seconds
        barrier.wait(timeout=60)
        samples=[s for future in futures for s in future.result()]
    wall=time.perf_counter()-clock['start']
    output=ROOT/'原始结果'
    output.mkdir(exist_ok=True)
    with (output/f'{tag}_samples.jsonl').open('w',encoding='utf-8') as file:
        for sample in samples: file.write(json.dumps(sample,ensure_ascii=False)+'\n')
    groups={}
    for category in CATEGORIES:
        rows=[s for s in samples if s['category']==category]
        groups[category]={'samples':len(rows),'failures':sum(not r['ok'] for r in rows),
                         'p95_all_ms':p95([r['ms'] for r in rows]),
                         'max_body_bytes':max((r['bytes'] for r in rows),default=0)}
    summary={'kind':'short_backend_HTTP_probe_not_AC06','concurrency':concurrency,'requested_seconds':seconds,'started_unix':clock['started_unix'],
             'wall_including_drain_seconds':wall,'setup_login_seconds_excluded':setup_seconds,
             'samples':len(samples),'failures':sum(not s['ok'] for s in samples),
             'successful_per_minute':sum(s['ok'] for s in samples)/wall*60,'groups':groups,
             'dataset_after':dataset_info(),'error_codes':sorted({s['code'] for s in samples if not s['ok']}),
             'server_threads':32,'timing_end':'full_response_body_received',
             'scope_note':'No browser/Vue rendering. Guarded diagnosis includes validation plus commit. One-record CSV per request; not an all-own-records export workload.'}
    (output/f'{tag}_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--concurrency',type=int,nargs='+',default=[10,60,360])
    parser.add_argument('--seconds',type=int,default=15)
    parser.add_argument('--tag',default='load')
    parser.add_argument('--seed-only',action='store_true')
    args=parser.parse_args()
    assert p95(list(range(1,21)))==19
    seed()
    print(json.dumps({'seed':dataset_info()},ensure_ascii=False),flush=True)
    if not args.seed_only:
        for n in args.concurrency: phase(n,args.seconds,f'{args.tag}_{n}')
