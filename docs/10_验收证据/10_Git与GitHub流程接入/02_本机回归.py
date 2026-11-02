"""Capture one fresh local check run; refuse to overwrite earlier evidence."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

stage = Path(__file__).resolve().parent
code = stage.parents[1] / '05_系统代码'
label = sys.argv[1]
if not label.replace('_', '').isalnum():
    raise ValueError('Use an alphanumeric evidence label')
output = stage / (label + '.json')
log = stage / (label + '.txt')
if output.exists() or log.exists():
    raise RuntimeError('Refuse to overwrite evidence')
env = {**os.environ, 'T22_APP_CONFIG': str(Path.home() / '.cache/t22-runtime/private/app.json'),
       'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}
checks = []
with log.open('x', encoding='utf-8') as stream:
    for args in [['check'], ['makemigrations', '--check', '--dry-run'], ['test', 'tests', 'governance', 'clinic', '--noinput', '-v', '2']]:
        start = time.perf_counter()
        result = subprocess.run([sys.executable, 'manage.py', *args], cwd=code, env=env,
                                stdout=stream, stderr=subprocess.STDOUT)
        checks.append({'args': args, 'exit_code': result.returncode, 'seconds': round(time.perf_counter() - start, 3)})
        if result.returncode:
            break
report = {'passed': len(checks) == 3 and all(row['exit_code'] == 0 for row in checks), 'checks': checks}
output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(report))
print(log.read_text(encoding='utf-8')[-900:])
raise SystemExit(0 if report['passed'] else 1)
