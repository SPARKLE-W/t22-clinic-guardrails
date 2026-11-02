import json
import os
import subprocess
import sys
import time
from pathlib import Path

stage = Path(__file__).resolve().parent.parent
code = stage.parent.parent / 'backend'
label = sys.argv[1]
output = stage / '原始结果'
output.mkdir(exist_ok=True)
if (output / (label + '.json')).exists():
    raise RuntimeError('Refuse to overwrite an evidence label')
env = {**os.environ, 'T22_APP_CONFIG': str(Path.home() / '.cache/t22-runtime/private/app.json'),
       'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}
args = sys.argv[2:] or ['test', 'tests', 'governance', 'clinic', '--noinput', '-v', '2']
command = [sys.executable, 'manage.py', *args]
start = time.perf_counter()
result = subprocess.run(command, cwd=code, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
(output / (label + '.txt')).write_text(result.stdout + result.stderr, encoding='utf-8')
metadata = {'label': label, 'command': command, 'exit_code': result.returncode, 'wall_seconds': round(time.perf_counter() - start, 3)}
(output / (label + '.json')).write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(metadata))
print((result.stdout + result.stderr)[-2200:])
raise SystemExit(result.returncode)
