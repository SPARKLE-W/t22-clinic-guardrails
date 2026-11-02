"""Capture real compile/verify commands; never overwrite an existing run."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

STAGE = Path(__file__).resolve().parent.parent
CODE = STAGE.parent.parent / '05_系统代码'
RUN = STAGE / '原始结果' / (sys.argv[1] if len(sys.argv) > 1 else 'cli_cases_01')
RUN.mkdir(exist_ok=False)
complete = json.loads((CODE / 'examples/01_完整定义需求_v0.2.json').read_text(encoding='utf-8'))
missing = copy.deepcopy(complete)
del missing['quality_gates']
conflict = copy.deepcopy(complete)
conflict['authorization']['projections']['R-AUDIT']['fields']['diagnosis'] = {'type': 'text'}
results = []
for label, value, expected_exit in [('complete', complete, 0), ('missing_quality', missing, 2), ('manager_clinical_conflict', conflict, 2)]:
    source = RUN / (label + '.input.json')
    source.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    command = [sys.executable, '-m', 'vetcontrols', 'compile', '--input', str(source), '--output', str(RUN / label)]
    result = subprocess.run(command, cwd=CODE, capture_output=True, text=True, encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    (RUN / (label + '.stdout.txt')).write_text(result.stdout + result.stderr, encoding='utf-8')
    report = json.loads((RUN / label / 'report.json').read_text(encoding='utf-8'))
    passed = result.returncode == expected_exit and report['ok'] == (expected_exit == 0) and (RUN / label / 'candidate.json').exists() == (expected_exit == 0)
    results.append({'label': label, 'command': command, 'exit_code': result.returncode, 'expected_exit': expected_exit,
                    'check_passed': passed, 'report': report})
command = [sys.executable, '-m', 'vetcontrols', 'verify', '--candidate', str(RUN / 'complete/candidate.json'), '--output', str(RUN / 'verified')]
result = subprocess.run(command, cwd=CODE, capture_output=True, text=True, encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
(RUN / 'verified.stdout.txt').write_text(result.stdout + result.stderr, encoding='utf-8')
results.append({'label': 'verify_complete', 'command': command, 'exit_code': result.returncode, 'expected_exit': 0,
                'check_passed': result.returncode == 0})
(RUN / 'summary.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps([{'label': item['label'], 'exit_code': item['exit_code'], 'check_passed': item['check_passed']} for item in results]))
raise SystemExit(0 if all(item['check_passed'] for item in results) else 1)
