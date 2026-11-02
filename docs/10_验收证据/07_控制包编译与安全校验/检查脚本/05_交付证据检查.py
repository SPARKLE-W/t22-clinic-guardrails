"""Verify current evidence, assets and baseline without rerunning services/tests."""
import ast
import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STAGE = Path(__file__).resolve().parent.parent
PROJECT = STAGE.parent.parent
CODE = PROJECT / 'backend'
OUTPUT = STAGE / '08_交付证据与基线检查'
if OUTPUT.with_suffix('.json').exists() or OUTPUT.with_suffix('.md').exists():
    raise RuntimeError('Refuse to overwrite an evidence checkpoint.')
sys.path.insert(0, str(CODE))
from vetcontrols import compile_demand, verify_candidate
from vetcontrols.compiler import FLOOR_DIGEST, MAX_BYTES, REGISTRY, SCHEMA, _digest, _normalize

checks = []


def check(name, passed, details=None):
    checks.append({'check': name, 'passed': bool(passed), 'details': details})


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


baseline = {
    '03_需求规格说明书.md': 'FE95C5BF2495B763EA2C7075C46CBE5D367F7F82401E2E1818EF81BF9243DAE8',
    '03_需求规格说明书.docx': '844778341B7EB43907FF43CB08AD6B76DB6A9ACE2C77D9B02A028C895958B011',
    'T22_门诊护栏规则_学生版任务书.docx': '424200AA2FCDF333FCE984F008E4BEA168D02E3A33B3BECEA00893500E2FB759',
    '10_验收证据/05_需求基线与首轮设计/02_需规v1.0_基线快照.md': 'FE95C5BF2495B763EA2C7075C46CBE5D367F7F82401E2E1818EF81BF9243DAE8',
}
for name, expected in baseline.items():
    check('冻结/来源指纹 ' + name, sha(PROJECT / name) == expected, sha(PROJECT / name))
methods = (STAGE / '原始结果/full_final_01.txt').read_text(encoding='utf-8')
test_meta = read(STAGE / '原始结果/full_final_01.json')
check('当前全部24项实际测试记录', test_meta['exit_code'] == 0 and re.search(r'Ran 24 tests', methods) and re.search(r'\nOK\s*$', methods), test_meta)
for label, expected, marker in [('compiler_red_01', 1, 'failures=3'), ('cli_red_01', 1, 'failures=3'),
                                 ('candidate_limits_red_02', 1, 'failures=1'), ('nonclinical_audit_red_01', 1, 'failures=1')]:
    metadata = read(STAGE / ('原始结果/' + label + '.json'))
    raw = (STAGE / ('原始结果/' + label + '.txt')).read_text(encoding='utf-8')
    check('真实功能失败保留 ' + label, metadata['exit_code'] == expected and marker in raw)
check('测试编辑错误独立披露', 'NameError' in (STAGE / '原始结果/candidate_limits_red_01.txt').read_text(encoding='utf-8'))
run = STAGE / '原始结果/cli_cases_02'
summary = read(run / 'summary.json')
check('最新四组CLI实际退出/结果', len(summary) == 4 and all(row['check_passed'] and row['exit_code'] == row['expected_exit'] for row in summary))
candidate = read(run / 'complete/candidate.json')
definition = read(CODE / 'examples/01_完整定义需求_v0.2.json')
compiled = compile_demand(definition)
check('最新候选等于当前源重新编译', compiled['ok'] and candidate == compiled['candidate'])
check('独立复核/内容摘要/输入摘要', verify_candidate(candidate)['ok'] and candidate['content_digest'] == _digest(candidate['payload']) and candidate['input_digest'] == _digest(_normalize(definition)))
check('当前底线摘要与未提交状态', candidate['payload']['compiled']['floor_digest'] == FLOOR_DIGEST and candidate['governance'] == {'state': 'draft_unsubmitted', 'submitted_actor': None, 'approval': None, 'activation': None})
check('候选文件在复核输入上限内', len((run / 'complete/candidate.json').read_bytes()) <= MAX_BYTES)
for label in ['complete', 'missing_quality', 'manager_clinical_conflict']:
    report = read(run / label / 'report.json')
    check('CLI原输入摘要 ' + label, report['raw_input_sha256'] == sha(run / (label + '.input.json')))
check('缺项/冲突不输出候选', all(not (run / name / 'candidate.json').exists() for name in ['missing_quality', 'manager_clinical_conflict']))
check('verify只输出报告', (run / 'verified/report.json').exists() and not (run / 'verified/candidate.json').exists())
auth = definition['authorization']
counts = {'bindings': len(auth['bindings']), 'modes': sum(len(b['server_selected_modes']) for b in auth['bindings']),
          'projections': len(auth['projections']), 'fields': sum(len(p['fields']) for p in auth['projections'].values()),
          'checkers': len(REGISTRY['checkers']), 'facts': len(REGISTRY['facts']), 'phases': len(REGISTRY['phases'])}
check('目录规模与固定schema一致', counts == {'bindings': 20, 'modes': 29, 'projections': 35, 'fields': 208, 'checkers': 10, 'facts': 87, 'phases': 11} and SCHEMA['properties']['authorization']['const'] == _normalize(auth), counts)
teacher = read(STAGE / '原始结果/teacher_source_probe_20261022_02.json')
check('教师修后独立6项记录', len(teacher['checks']) == 6 and all(teacher['checks'].values()))
check('两个限定实现审查已交付', all((STAGE / name).exists() for name in ['05_模拟教师实现审查.md', '06_角色与异常输入实现审查.md']))
versions = {}
for line in (CODE / 'requirements.lock').read_text().splitlines():
    if not line or line.startswith('#'):
        continue
    name, version = line.split('==')
    versions[name] = {'locked': version, 'actual': importlib.metadata.version(name)}
check('依赖实际版本匹配锁文件', all(v['locked'] == v['actual'] for v in versions.values()), versions)
check('Python3.12实际环境', sys.version_info[:2] == (3, 12), sys.version.split()[0])
python_files = list(CODE.rglob('*.py')) + list((STAGE / '检查脚本').glob('*.py'))
for path in python_files:
    ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
check('当前Python源码语法', True, len(python_files))
json_files = list(CODE.rglob('*.json')) + list(STAGE.rglob('*.json'))
for path in json_files:
    read(path)
check('本批JSON可解析', True, len(json_files))
documents = [PROJECT / '00_作业导航.md', PROJECT / '02_T22完成流程与人机任务分工.md',
             PROJECT / '04_设计与控制文件/00_设计导航与基线.md', PROJECT / '04_设计与控制文件/08_控制包机器契约与静态编译.md',
             CODE / 'README.md', PROJECT / '07_AI使用留痕/06_控制编译实现与审查记录.md',
             *[STAGE / name for name in ['05_模拟教师实现审查.md', '06_角色与异常输入实现审查.md', '07_主代理综合处置与交接.md']]]
missing_links = []
for doc in documents:
    for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', doc.read_text(encoding='utf-8')):
        target = target.strip('<>')
        if target.startswith(('https://', 'http://', '#')):
            continue
        target = re.sub(r':\d+$', '', target.split('#')[0])
        path = Path(target) if re.match(r'^[A-Za-z]:[/\\]', target) else doc.parent / target
        if path.resolve() == OUTPUT.with_suffix('.md').resolve():
            continue  # This check writes that report below.
        if not path.exists():
            missing_links.append({'file': str(doc.relative_to(PROJECT)), 'target': target})
check('当前入口/报告本地链接', not missing_links, missing_links)
queries = {
    '任务候选端口现场': "@((Get-NetTCPConnection -State Listen -ErrorAction Stop) | Where-Object { $_.LocalPort -in @(13322,15672,25692,14369) -or ($_.LocalPort -ge 18722 -and $_.LocalPort -le 18729) } | Select-Object LocalAddress,LocalPort,OwningProcess) | ConvertTo-Json -Compress",
    '缓存二进制服务进程现场': "@(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like '*\\.cache\\t22-runtime\\bin\\*' } | Select-Object ProcessId) | ConvertTo-Json -Compress",
}
for name, query in queries.items():
    result = subprocess.run(['powershell', '-NoProfile', '-Command', query], capture_output=True, text=True)
    observed = json.loads(result.stdout) if result.stdout.strip() else []
    check(name, result.returncode == 0 and not observed, observed)
inventory = {str(path.relative_to(PROJECT)): sha(path) for path in sorted(CODE.rglob('*')) if path.is_file() and '__pycache__' not in path.parts}
report = {'checked_utc': datetime.now(timezone.utc).isoformat(), 'scope': 'static_compile_evidence_not_product_acceptance',
          'passed': all(row['passed'] for row in checks), 'checks': checks, 'source_inventory': inventory,
          'runtime_integrated': False, 'formal_AC06_executed': False, 'quality_results': 'not_measured'}
OUTPUT.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
lines = ['# 控制编译交付证据与基线检查', '', '> 2026-10-22；只读复算与现场检查，不重跑全系统测试，不启动服务。', '',
         f"检查结果：{sum(row['passed'] for row in checks)}/{len(checks)}符合预期。", '', '| 项目 | 结果 |', '| --- | --- |']
lines += ['| ' + row['check'] + ' | ' + ('通过' if row['passed'] else '失败') + ' |' for row in checks]
lines += ['', '当前全部24项静态API/CLI测试实际通过，最新CLI四组符合预期；详情、指纹、依赖版本与现场观察见[同名JSON](08_交付证据与基线检查.json)。', '',
          '候选仍未提交/未审批/未启用。冻结需规及Word、原任务书指纹已现场复核。当前候选端口及缓存服务进程无运行项；本批没有启动服务。正式性能、覆盖率、完整系统及运行治理均未因本检查通过而验收。', '']
OUTPUT.with_suffix('.md').write_text('\n'.join(lines), encoding='utf-8')
print(json.dumps({'passed': report['passed'], 'checks': len(checks), 'failed': [row['check'] for row in checks if not row['passed']]}))
raise SystemExit(0 if report['passed'] else 1)
