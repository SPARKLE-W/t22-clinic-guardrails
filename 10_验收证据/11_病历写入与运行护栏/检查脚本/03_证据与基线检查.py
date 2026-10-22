import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

stage = Path(__file__).resolve().parents[1]
project = stage.parents[1]
code = project / '05_系统代码'
output = stage / '04_证据与基线检查.json'
if output.exists():
    raise RuntimeError('Refuse to overwrite evidence')
checks = []


def check(name, value):
    checks.append({'check': name, 'passed': bool(value)})


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


frozen = {
    '03_需求规格说明书.md': 'FE95C5BF2495B763EA2C7075C46CBE5D367F7F82401E2E1818EF81BF9243DAE8',
    '03_需求规格说明书.docx': '844778341B7EB43907FF43CB08AD6B76DB6A9ACE2C77D9B02A028C895958B011',
    'T22_门诊护栏规则_学生版任务书.docx': '424200AA2FCDF333FCE984F008E4BEA168D02E3A33B3BECEA00893500E2FB759',
    '10_验收证据/05_需求基线与首轮设计/02_需规v1.0_基线快照.md': 'FE95C5BF2495B763EA2C7075C46CBE5D367F7F82401E2E1818EF81BF9243DAE8',
}
for name, digest in frozen.items():
    check('冻结/原件指纹 ' + name, hashlib.sha256((project / name).read_bytes()).hexdigest().upper() == digest)
full_log = stage / '原始结果/full_final_01.txt'
check('完整78项MySQL回归', load(stage / '原始结果/full_final_01.json')['exit_code'] == 0 and 'Ran 78 tests' in full_log.read_text(encoding='utf-8'))
check('两项审查缺陷先失败', load(stage / '原始结果/review_findings_red_01.json')['exit_code'] == 1 and 'FAILED (failures=2)' in (stage / '原始结果/review_findings_red_01.txt').read_text(encoding='utf-8'))
check('修复后19项相关回归', load(stage / '原始结果/review_findings_green_01.json')['exit_code'] == 0 and 'Ran 19 tests' in (stage / '原始结果/review_findings_green_01.txt').read_text(encoding='utf-8'))
http = load(stage / '原始结果/http_01.json')
check('19项实际HTTP检查', http['passed'] and len(http['checks']) == 19 and all(row['passed'] for row in http['checks']))
ui = load(stage / '原始结果/final_ui_01.json')
check('1366x768实际页面及诊断告警', ui['passed'] and '正式病历已保存' in (stage / '原始结果/browser_doctor_final_01.txt').read_text(encoding='utf-8') and '本次提交被护栏拦截' in (stage / '原始结果/browser_guard_01.txt').read_text(encoding='utf-8'))
sources = [p for p in code.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix in {'.py', '.html', '.json', '.in', '.lock'}]
check('回归后被测源码未再变更', all(path.stat().st_mtime <= full_log.stat().st_mtime for path in sources))
for path in [p for p in sources if p.suffix == '.py'] + list((stage / '检查脚本').glob('*.py')):
    ast.parse(path.read_text(encoding='utf-8'))
check('Python源码及检查脚本语法', True)
for path in [code / 'governance/templates/governance/home.html', code / 'clinic/templates/clinic/workbench_script.html', code / 'clinic/templates/clinic/clinical_write_script.html']:
    script = re.search(r'<script>(.*?)</script>', path.read_text(encoding='utf-8'), re.S).group(1)
    check('页面脚本语法 ' + path.name, subprocess.run(['node', '--check', '-'], input=script, text=True, capture_output=True).returncode == 0)
broken = []
for path in [project / '00_作业导航.md', project / '02_T22完成流程与人机任务分工.md', project / 'README.md', code / 'README.md', project / '04_设计与控制文件/00_设计导航与基线.md', stage / '03_本批结果与后续.md']:
    for link in re.findall(r'\[[^\]]+\]\(([^)]+)\)', path.read_text(encoding='utf-8')):
        if link.startswith(('https://', 'http://', '#')):
            continue
        value = re.sub(r':\d+$', '', link.split('#')[0]).strip('<>')
        target = Path(value) if re.match(r'^[A-Za-z]:[/\\]', value) else path.parent / value
        if not target.exists():
            broken.append(value)
check('当前入口链接', not broken)
report = {'passed': all(row['passed'] for row in checks), 'checks': checks, 'broken_links': broken, 'subagent_calls': 1,
          'scope': 'diagnosis_and_durable_alert_slice_not_MQ_or_full_acceptance',
          'source_sha256': {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest().upper() for path in sources}}
output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'passed': report['passed'], 'checks': len(checks), 'failed': [row['check'] for row in checks if not row['passed']]}))
raise SystemExit(0 if report['passed'] else 1)
