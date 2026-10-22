"""Capture real test output and exit code; redact no credentials because none are emitted."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

here = Path(__file__).resolve().parent
output = here.parent/'原始结果'
output.mkdir(exist_ok=True)
tag = sys.argv[1]
labels = sys.argv[2:] or ['probe']
cmd = [sys.executable,'manage.py','test',*labels,'--keepdb','--noinput','--verbosity=2']
env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
started = time.time()
run = subprocess.run(cmd,cwd=here,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
text = run.stdout.decode('utf-8','replace')
(output/f'{tag}.txt').write_text(text,encoding='utf-8')
summary = {'label':tag,'command':cmd[1:],'exit_code':run.returncode,'wall_seconds':round(time.time()-started,3),'runtime_tests':True}
(output/f'{tag}.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(text[-6500:])
print(json.dumps(summary,ensure_ascii=False))
sys.exit(run.returncode)
