import ast
import hashlib
import importlib.metadata
import json
import re
import subprocess
from pathlib import Path

stage = Path(__file__).resolve().parent.parent
project = stage.parent.parent
code = project / '05_系统代码'
output = stage / '03_证据与基线检查.json'
if output.exists():
    raise RuntimeError('Refuse to overwrite an evidence checkpoint')
checks = []


def check(name, passed, details=None):
    checks.append({'check': name, 'passed': bool(passed), 'details': details})


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


for file, expected in {
    '03_需求规格说明书.md': 'FE95C5BF2495B763EA2C7075C46CBE5D367F7F82401E2E1818EF81BF9243DAE8',
    '03_需求规格说明书.docx': '844778341B7EB43907FF43CB08AD6B76DB6A9ACE2C77D9B02A028C895958B011',
    'T22_门诊护栏规则_学生版任务书.docx': '424200AA2FCDF333FCE984F008E4BEA168D02E3A33B3BECEA00893500E2FB759',
}.items():
    check('冻结/来源指纹 ' + file, sha(project / file) == expected, sha(project / file))
raw = (stage / '原始结果/full_final_02.txt').read_text(encoding='utf-8')
check('最新44项全量实际记录', load(stage / '原始结果/full_final_02.json')['exit_code'] == 0 and 'Ran 44 tests' in raw and re.search(r'\nOK\s*', raw))
for label, expected in [('governance_red_01', 'failures=2'), ('missing_gate_red_01', 'failures=1'), ('audit_scope_red_01', 'failures=1')]:
    check('功能失败证据保留 ' + label, load(stage / ('原始结果/' + label + '.json'))['exit_code'] == 1 and expected in (stage / ('原始结果/' + label + '.txt')).read_text(encoding='utf-8'))
http = load(stage / '原始结果/http_final_source_01.json')
check('最新14项HTTP/持久化实际记录', http['passed'] and len(http['checks']) == 14 and all(row['passed'] for row in http['checks']))
check('应用重启后记录保留', load(stage / '原始结果/http_after_app_restart_01.json')['passed'])
for label in ['app_final_start_02', 'app_final_stop_03']:
    check('修后实际启停 ' + label, load(stage / ('原始结果/' + label + '.json'))['exit_code'] == 0)
check('启停竞态原失败未隐藏', load(stage / '原始结果/app_final_stop_01.json')['exit_code'] == 1)
browser = (stage / '原始结果/browser_final_1366.txt').read_text(encoding='utf-8')
check('真实浏览器最终状态/视口留存', all(value in browser for value in ['1366', '768', 'scrollWidth', '当前生效', 'activateButtons']))
versions = {line.split('==')[0]: line.split('==')[1] for line in (code / 'requirements.lock').read_text().splitlines() if line and not line.startswith('#')}
check('实际依赖匹配锁文件', all(importlib.metadata.version(name) == version for name, version in versions.items()), versions)
sources = [path for path in code.rglob('*') if path.is_file() and '__pycache__' not in path.parts]
for path in [p for p in sources if p.suffix == '.py'] + list((stage / '检查脚本').glob('*.py')):
    ast.parse(path.read_text(encoding='utf-8'))
check('当前Python源码及迁移语法', True)
template = (code / 'governance/templates/governance/home.html').read_text(encoding='utf-8')
script = re.search(r'<script>(.*?)</script>', template, re.S).group(1)
js = subprocess.run(['node', '--check', '-'], input=script, text=True, capture_output=True)
check('当前页面脚本语法', js.returncode == 0)
private = Path.home() / '.cache/t22-runtime/private'
config = load(private / 'app.json')
secrets = [config['secret_key'], config['database']['PASSWORD']] + [row['password'] for row in load(private / 'app-demo-accounts.json')]
files = sources + [p for p in stage.rglob('*') if p.is_file()]
hits = [str(p.relative_to(project)) for p in files if any(secret.encode() in p.read_bytes() for secret in secrets)]
check('真实数据库/会话密钥/演示口令未进入源及本批证据', not hits, hits)
documents = [project / '00_作业导航.md', project / '02_T22完成流程与人机任务分工.md', code / 'README.md',
             project / '04_设计与控制文件/09_治理运行接入与账号基础.md', stage / '02_本批结果与后续.md']
broken = []
for doc in documents:
    for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)', doc.read_text(encoding='utf-8')):
        if target.startswith(('https://', 'http://', '#')):
            continue
        target = re.sub(r':\d+$', '', target.split('#')[0]).strip('<>')
        path = Path(target) if re.match(r'^[A-Za-z]:[/\\]', target) else doc.parent / target
        if path.resolve() == output.with_suffix('.md').resolve():
            continue
        if not path.exists():
            broken.append({'file': str(doc.relative_to(project)), 'target': target})
check('当前入口与新报告链接', not broken, broken)
query = "@{ports=@(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object {$_.LocalPort -in @(13322,15672,25692,14369,18730) -or ($_.LocalPort -ge 18722 -and $_.LocalPort -le 18729)} | Select-Object LocalPort,OwningProcess); services=@(Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -like '*\\.cache\\t22-runtime\\bin\\*' -or ($_.Name -match '^python(w)?\\.exe$' -and $_.CommandLine -match 'manage.py.*runserver.*127.0.0.1:18730')} | Select-Object ProcessId)} | ConvertTo-Json -Depth 4 -Compress"
status = subprocess.run(['powershell', '-NoProfile', '-Command', query], capture_output=True, text=True)
state = json.loads(status.stdout) if status.stdout.strip() else None
check('最终任务端口及缓存服务现场已关闭', status.returncode == 0 and state is not None and not state['ports'] and not state['services'], state)
report = {'passed': all(row['passed'] for row in checks), 'checks': checks,
          'source_inventory': {str(path.relative_to(project)): sha(path) for path in sources},
          'scope': 'governance_slice_evidence_not_full_product_acceptance', 'subagent_calls': 0,
          'formal_AC06_executed': False, 'real_human_approval': False}
output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
lines = ['# 治理接入证据与基线检查', '', '> 2026-10-22；主代理检查，不新增子代理审查，不重复运行压测。', '',
         f"{sum(row['passed'] for row in checks)}/{len(checks)}项符合预期。", '', '| 检查 | 结果 |', '| --- | --- |']
lines += ['| ' + row['check'] + ' | ' + ('通过' if row['passed'] else '失败') + ' |' for row in checks]
lines += ['', '详情、当前源码指纹和版本见[同名JSON](03_证据与基线检查.json)。44项回归与14项最新HTTP核对为本批实际结果，未冒称全系统、正式性能或真实人工批准通过。Web及MySQL已关闭，合成数据与私密配置保留。', '']
output.with_suffix('.md').write_text('\n'.join(lines), encoding='utf-8')
print(json.dumps({'passed': report['passed'], 'checks': len(checks), 'failed': [row['check'] for row in checks if not row['passed']]}))
raise SystemExit(0 if report['passed'] else 1)
