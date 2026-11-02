import json
import subprocess
import sys
from pathlib import Path

stage = Path(__file__).resolve().parent.parent
action, label = sys.argv[1:3]
if action not in ['start', 'stop']:
    raise ValueError('start or stop required')
output = stage / ('原始结果/' + label + '.json')
if output.exists():
    raise RuntimeError('Refuse to overwrite lifecycle evidence')
script = stage.parent.parent / '05_系统代码/运行脚本/01_本地应用.ps1'
command = ['powershell', '-NoProfile', '-File', str(script), action]
stdout_path = output.with_suffix('.stdout.txt')
stderr_path = output.with_suffix('.stderr.txt')
# File-backed handles avoid inherited pipe handles keeping communicate() open
# after Windows PowerShell has launched a background Python child.
with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=40)
output.write_text(json.dumps({'command': command, 'action': action, 'exit_code': result.returncode,
                              'stdout': stdout_path.read_text(encoding='utf-8', errors='replace'),
                              'stderr': stderr_path.read_text(encoding='utf-8', errors='replace')}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'action': action, 'exit_code': result.returncode}))
raise SystemExit(result.returncode)
