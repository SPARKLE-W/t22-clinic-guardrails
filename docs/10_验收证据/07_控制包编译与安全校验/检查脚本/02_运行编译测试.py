"""Capture actual unittest output and exit status without starting any service."""
import json
import subprocess
import sys
import time
from pathlib import Path

STAGE = Path(__file__).resolve().parent.parent
CODE = STAGE.parent.parent / 'backend'
label = sys.argv[1]
targets = sys.argv[2:] or ['tests.test_controls']
output = STAGE / '原始结果'
output.mkdir(exist_ok=True)
if (output / (label + '.json')).exists():
    raise RuntimeError('Refuse to overwrite a test run label.')
started = time.perf_counter()
command = [sys.executable, '-m', 'unittest', '-v', *targets]
result = subprocess.run(command, cwd=CODE, capture_output=True, encoding='utf-8', errors='replace', env={**__import__('os').environ, 'PYTHONIOENCODING':'utf-8'})
(output / (label + '.txt')).write_text(result.stdout + result.stderr, encoding='utf-8')
metadata = {'label':label, 'command':command, 'exit_code':result.returncode, 'wall_seconds':round(time.perf_counter()-started,3)}
(output / (label + '.json')).write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(metadata,ensure_ascii=False))
print((result.stdout+result.stderr)[-2500:])
raise SystemExit(result.returncode)
