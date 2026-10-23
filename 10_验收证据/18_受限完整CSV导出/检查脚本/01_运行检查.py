import json
import os
import subprocess
import sys
import time
from pathlib import Path

stage=Path(__file__).resolve().parents[1]
code=stage.parents[1]/'05_系统代码'
label=sys.argv[1]
if not label.replace('_','').isalnum():
    raise ValueError('Invalid evidence label')
output=stage/'原始结果'
output.mkdir(exist_ok=True)
log,metadata=output/(label+'.txt'),output/(label+'.json')
if log.exists() or metadata.exists():
    raise RuntimeError('Refuse to overwrite evidence')
env={**os.environ,'T22_APP_CONFIG':str(Path.home()/'.cache/t22-runtime/private/app.json'),
     'T22_MQ_CONFIG':str(Path.home()/'.cache/t22-runtime/private/app-mq.json'),
     'PYTHONIOENCODING':'utf-8','PYTHONDONTWRITEBYTECODE':'1'}
arguments=sys.argv[2:] or ['test','tests','governance','clinic','--noinput','-v2']
if arguments[0]=='test':
    # Only TEST.NAME changes. The main and other workers' databases are never flushed.
    script="import os,sys;os.environ.setdefault('DJANGO_SETTINGS_MODULE','vetapp.settings');from django.conf import settings;settings.DATABASES['default']['TEST']['NAME']='t22_app_mq_test';from django.core.management import execute_from_command_line;execute_from_command_line(['manage.py',*sys.argv[1:]])"
    command=[sys.executable,'-c',script,*arguments]
else:
    command=[sys.executable,'manage.py',*arguments]
start=time.perf_counter()
with log.open('x',encoding='utf-8') as stream:
    result=subprocess.run(command,cwd=code,env=env,stdout=stream,stderr=subprocess.STDOUT)
report={'arguments':arguments,'test_database':'t22_app_mq_test' if arguments[0]=='test' else None,
        'exit_code':result.returncode,'wall_seconds':round(time.perf_counter()-start,3)}
metadata.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(report))
print(log.read_text(encoding='utf-8')[-1500:])
raise SystemExit(result.returncode)
