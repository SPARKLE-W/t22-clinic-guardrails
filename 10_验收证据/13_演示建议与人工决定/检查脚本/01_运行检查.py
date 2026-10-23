import json
import os
import subprocess
import sys
import time
from pathlib import Path

stage = Path(__file__).resolve().parents[1]
code = stage.parents[1] / '05_系统代码'
label = sys.argv[1]
if not label.replace('_', '').isalnum():
    raise ValueError('Invalid evidence label')
output = stage / '原始结果'
output.mkdir(exist_ok=True)
log, metadata = output / (label + '.txt'), output / (label + '.json')
if log.exists() or metadata.exists():
    raise RuntimeError('Refuse to overwrite evidence')
env = {**os.environ, 'T22_APP_CONFIG': str(Path.home() / '.cache/t22-runtime/private/app.json'),
       'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}
command = [sys.executable, 'manage.py', *(sys.argv[2:] or ['test', 'tests', 'governance', 'clinic', '--noinput', '-v', '2'])]
start = time.perf_counter()
with log.open('x', encoding='utf-8') as stream:
    result = subprocess.run(command, cwd=code, env=env, stdout=stream, stderr=subprocess.STDOUT)
report = {'command': command, 'exit_code': result.returncode, 'wall_seconds': round(time.perf_counter() - start, 3)}
metadata.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(report))
print(log.read_text(encoding='utf-8')[-1600:])
raise SystemExit(result.returncode)
