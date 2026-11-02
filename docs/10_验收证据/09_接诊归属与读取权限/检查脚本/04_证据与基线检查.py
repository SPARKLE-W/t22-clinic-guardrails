import ast
import hashlib
import importlib.metadata
import json
import re
import subprocess
from pathlib import Path

stage = Path(__file__).resolve().parent.parent
project = stage.parent.parent
code = project / 'backend'
output = stage / '03_证据与基线检查.json'
if output.exists():
    raise RuntimeError('Refuse to overwrite evidence')
checks = []
def check(name, value, details=None):
    checks.append({'check': name, 'passed': bool(value), 'details': details})
def load(path):
    return json.loads(path.read_text(encoding='utf-8'))
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()

for file, expected in {
    '03_需求规格说明书.md': 'FE95C5BF2495B763EA2C7075C46CBE5D367F7F82401E2E1818EF81BF9243DAE8',
    '03_需求规格说明书.docx': '844778341B7EB43907FF43CB08AD6B76DB6A9ACE2C77D9B02A028C895958B011',
    'T22_门诊护栏规则_学生版任务书.docx': '424200AA2FCDF333FCE984F008E4BEA168D02E3A33B3BECEA00893500E2FB759',
}.items():
    check('冻结/来源指纹 '+file, sha(project/file) == expected)
raw = (stage/'原始结果/full_final_01.txt').read_text(encoding='utf-8')
check('真实59项回归记录', load(stage/'原始结果/full_final_01.json')['exit_code'] == 0 and 'Ran 59 tests' in raw and '\nOK\n' in raw)
for label, marker in [('clinic_red_01','failures=2'),('refused_record_index_red_01','failures=1')]:
    check('真实功能失败保留 '+label, load(stage/('原始结果/'+label+'.json'))['exit_code'] == 1 and marker in (stage/('原始结果/'+label+'.txt')).read_text(encoding='utf-8'))
http = load(stage/'原始结果/http_clinic_01.json')
check('18项实际HTTP/数据核对', len(http['checks']) == 18 and http['passed'] and all(row['passed'] for row in http['checks']))
browser = (stage/'原始结果/browser_doctor_1366.txt').read_text(encoding='utf-8')
check('前台/医生真实页面记录', (stage/'原始结果/browser_front_created.txt').exists() and all(s in browser for s in ['尚无正式病历','1366','768','mutationButtons']))
sources = [p for p in code.rglob('*') if p.is_file() and '__pycache__' not in p.parts]
for path in [p for p in sources if p.suffix == '.py']+list((stage/'检查脚本').glob('*.py')):
    ast.parse(path.read_text(encoding='utf-8'))
check('源码/正式迁移语法', True)
for path in [code/'governance/templates/governance/home.html',code/'clinic/templates/clinic/workbench_script.html']:
    script = re.search(r'<script>(.*?)</script>', path.read_text(encoding='utf-8'), re.S).group(1)
    result = subprocess.run(['node','--check','-'], input=script, capture_output=True, text=True)
    check('最终页面脚本语法 '+path.name, result.returncode == 0)
versions = {line.split('==')[0]:line.split('==')[1] for line in (code/'requirements.lock').read_text().splitlines() if line and not line.startswith('#')}
check('实际依赖与锁一致', all(importlib.metadata.version(name) == version for name,version in versions.items()))
private = Path.home()/'.cache/t22-runtime/private'
config = load(private/'app.json')
secrets = [config['secret_key'],config['database']['PASSWORD']]+[row['password'] for row in load(private/'app-demo-accounts.json')]
files = sources+[p for p in stage.rglob('*') if p.is_file()]
leaks = [str(p.relative_to(project)) for p in files if any(s.encode() in p.read_bytes() for s in secrets)]
check('真实私密值未进入代码和本批证据', not leaks, leaks)
docs = [project/'00_作业导航.md',project/'02_T22完成流程与人机任务分工.md',code/'README.md',project/'04_设计与控制文件/10_接诊归属与读取权限实现.md',stage/'02_本批结果与后续.md']
broken = []
for doc in docs:
    for value in re.findall(r'\[[^\]]+\]\(([^)]+)\)',doc.read_text(encoding='utf-8')):
        if value.startswith(('http://','https://','#')):
            continue
        value = re.sub(r':\d+$','',value.split('#')[0]).strip('<>')
        target = Path(value) if re.match(r'^[A-Za-z]:[/\\]',value) else doc.parent/value
        if target.resolve() != output.with_suffix('.md').resolve() and not target.exists():
            broken.append({'file':str(doc.relative_to(project)),'target':value})
check('当前入口与报告链接', not broken, broken)
query = r"@{ports=@(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object {$_.LocalPort -in @(13322,15672,25692,14369,18730) -or ($_.LocalPort -ge 18722 -and $_.LocalPort -le 18729)} | Select-Object LocalPort,OwningProcess); services=@(Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -like '*\.cache\t22-runtime\bin\*' -or ($_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match 'manage.py.*runserver.*127.0.0.1:18730')} | Select-Object ProcessId)} | ConvertTo-Json -Depth 4 -Compress"
status = subprocess.run(['powershell','-NoProfile','-Command',query],capture_output=True,text=True)
state = json.loads(status.stdout) if status.stdout.strip() else None
check('最终任务端口与服务已关闭', status.returncode == 0 and state is not None and not state['ports'] and not state['services'], state)
report = {'passed':all(row['passed'] for row in checks),'checks':checks,'subagent_calls':0,
          'source_inventory':{str(p.relative_to(project)):sha(p) for p in sources},
          'scope':'reception_and_clinical_read_slice_not_full_clinical_write_or_performance_acceptance',
          'post_suite_change':'UI page button boundary only; final JS syntax checked', 'formal_AC06_executed':False}
output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
lines=['# 接诊读取证据与基线检查','','> 2026-10-22；主代理直接核对，无子代理轮审。','',f"{sum(r['passed'] for r in checks)}/{len(checks)}项符合预期。",'','| 检查 | 结果 |','| --- | --- |']
lines += ['| '+row['check']+' | '+('通过' if row['passed'] else '失败')+' |' for row in checks]
lines += ['','当前源码指纹/详情见[同名JSON](03_证据与基线检查.json)。59项回归后仅调整分页按钮状态，后端未变，补最终脚本语法核对。18项HTTP核对使用真实回环应用及合成数据；不冒称正式诊疗写入、全浏览器兼容或性能达标。最终Web/MySQL已关闭，数据缓存保留。','']
output.with_suffix('.md').write_text('\n'.join(lines),encoding='utf-8')
print(json.dumps({'passed':report['passed'],'checks':len(checks),'failed':[row['check'] for row in checks if not row['passed']]}))
raise SystemExit(0 if report['passed'] else 1)
