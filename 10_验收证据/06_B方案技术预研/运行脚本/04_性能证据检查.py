"""Verify this increment without rewriting the preceding 08 checkpoint."""
import ast
import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

STAGE = Path(__file__).resolve().parent.parent
PROJECT = STAGE.parent.parent
RAW = STAGE / '原始结果'
PROFILE = RAW / '性能定位'
RUNTIME = Path.home() / '.cache/t22-runtime'
parser = argparse.ArgumentParser()
parser.add_argument('--report-prefix', choices=['12_性能改进证据与基线检查', '16_四工作进程证据与基线检查'], default='12_性能改进证据与基线检查')
args = parser.parse_args()
prefix = args.report_prefix
checks, metrics = [], []


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def check(name, valid, details=None):
    checks.append({'check': name, 'passed': bool(valid), 'details': details})


def p95(values):
    return sorted(values)[math.ceil(len(values) * 0.95) - 1] if values else None


for name, expected in {
    '03_需求规格说明书.md': 'FE95C5BF2495B763EA2C7075C46CBE5D367F7F82401E2E1818EF81BF9243DAE8',
    '03_需求规格说明书.docx': '844778341B7EB43907FF43CB08AD6B76DB6A9ACE2C77D9B02A028C895958B011',
    '10_验收证据/05_需求基线与首轮设计/02_需规v1.0_基线快照.md': 'FE95C5BF2495B763EA2C7075C46CBE5D367F7F82401E2E1818EF81BF9243DAE8',
}.items():
    check('冻结基线: ' + name, sha(PROJECT / name) == expected)

for tag, count, exit_code in [('export_scope_query_budget_red', 1, 1), ('export_scope_batch_green', 3, 0), ('batch_export_full_regression', 40, 0)]:
    log = (RAW / (tag + '.txt')).read_text(encoding='utf-8-sig')
    metadata = read(RAW / (tag + '.json'))
    check('实际测试记录: ' + tag, metadata['exit_code'] == exit_code and f'Ran {count} test' in log
          and ('FAILED (failures=1)' in log if exit_code else re.search(r'^OK\s*$', log, re.M)),
          {'methods': count, 'exit_code': metadata['exit_code'], 'wall_seconds': metadata['wall_seconds']})
old_log = (RAW / 'export_scope_query_budget_red.txt').read_text(encoding='utf-8-sig')
new_log = (RAW / 'export_scope_batch_green.txt').read_text(encoding='utf-8-sig')
check('大集合N+1红绿证据', '"sql_statements": 8010' in old_log and '"sql_statements": 12' in new_log
      and '"mysql_errno": 1205' in new_log and '"audit_targets": 4000' in new_log)
check('修改前业务源保留', sha(PROFILE / 'services_before_batch_export.py') == '865863539AF0A8BA6EB4ADA1BD4FAB62D860336CED08EACFA5BE233EA06FC2F9')

for path in sorted(PROFILE.glob('*_summary.json')):
    summary = read(path)
    rows = [json.loads(line) for line in path.with_name(path.name.replace('_summary.json', '_samples.jsonl')).read_text().splitlines() if line.strip()]
    failures = sum(not row['ok'] for row in rows)
    valid = len(rows) == summary['samples'] and failures == summary['failures']
    groups = {}
    for category, expected in summary['groups'].items():
        selected = [row for row in rows if row['category'] == category]
        value = p95([row['ms'] for row in selected])
        valid &= len(selected) == expected['samples'] and math.isclose(value, expected['p95_all_ms'], abs_tol=1e-6)
        for field, timing in [('p95_app_ms', 'app'), ('p95_sql_ms', 'sql'), ('p95_commit_ms', 'commit'), ('p95_dispatch_ms', 'queue')]:
            actual = p95([row['timing'][timing] for row in selected if timing in row['timing']])
            valid &= actual == expected[field]
        groups[category] = {'samples': len(selected), 'p95_complete_ms': value,
            'timing_coverage': sum('app' in row['timing'] for row in selected),
            'p95_before_app_ms': p95([row['before_app_ms'] for row in selected if 'before_app_ms' in row]),
            'p95_after_app_ms': p95([row['after_app_ms'] for row in selected if 'after_app_ms' in row])}
    throughput = (len(rows) - failures) * 60 / summary['wall_including_drain_seconds']
    valid &= math.isclose(throughput, summary['successful_per_minute'], abs_tol=1e-6)
    events = sorted([(row['started_s'], 1) for row in rows] + [(row['ended_s'], -1) for row in rows])
    inflight = peak = 0
    for _, change in events:
        inflight += change
        peak = max(peak, inflight)
    workers = len({row['worker'] for row in rows})
    valid &= workers == peak == summary['concurrency']
    detail = {'tag': path.stem, 'samples': len(rows), 'failures': failures, 'global_workers': workers,
              'peak_client_inflight': peak, 'successful_per_minute': throughput, 'groups': groups}
    metrics.append(detail)
    check('逐请求及并发复算: ' + path.stem, valid, detail)

large = read(PROFILE / 'large_4000_HTTP_batch.json')
check('完整4000份真实HTTP与审计', large['records'] == large['audit_target_count'] == 4000
      and large['complete_scope_versions_history_and_audit'] and large['http_status'] == 200
      and large['utf8_bytes'] == 3093219, large)

files = [path for path in (STAGE / '实验代码').rglob('*.py') if '__pycache__' not in path.parts]
files += list((STAGE / '运行脚本').glob('*.py'))
for path in files:
    ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
check('当前Python源语法', True, {'files': len(files)})

markdown = list(STAGE.glob('*.md')) + list((PROJECT / '04_设计与控制文件').glob('*.md'))
markdown += [PROJECT / '00_作业导航.md', PROJECT / '02_T22完成流程与人机任务分工.md', PROJECT / '07_AI使用留痕/05_B方案预研协作与实际结果.md']
broken = []
for path in markdown:
    for target in re.findall(r'\]\(([^\n)]+)\)', path.read_text(encoding='utf-8-sig')):
        target = unquote(target.strip('<>').split('#')[0])
        if not target or re.match(r'^(https?://|mailto:|codex:)', target):
            continue
        target = re.sub(r':\d+$', '', target)  # Codex local file line suffix
        linked = Path(target)
        if not linked.is_absolute():
            linked = path.parent / linked
        if not linked.exists():
            broken.append({'source': str(path.relative_to(PROJECT)), 'target': target})
check('当前报告与导航本地链接', not broken, broken)

credentials = read(RUNTIME / 'private/connections.json')
secrets = [credentials['mysql']['password'], credentials['rabbitmq']['password'], (RUNTIME / 'private/django.key').read_text().strip()]
public_files = [p for p in STAGE.rglob('*') if p.is_file() and '__pycache__' not in p.parts]
hits = [str(p.relative_to(STAGE)) for p in public_files if any(s and s.encode() in p.read_bytes() for s in secrets)]
check('已知连接密码和Django密钥精确扫描', not hits, {'files': len(public_files), 'matching_files': hits, 'scope': 'known exact values only'})

query = "@((Get-NetTCPConnection -State Listen -ErrorAction Stop) | Where-Object { $_.LocalPort -in @(13322,15672,25692,14369) -or ($_.LocalPort -ge 18722 -and $_.LocalPort -le 18729) } | Select-Object LocalAddress,LocalPort,OwningProcess) | ConvertTo-Json -Compress"
r = subprocess.run(['powershell', '-NoProfile', '-Command', query], capture_output=True, text=True)
listeners = json.loads(r.stdout) if r.stdout.strip() else []
check('最终任务全部候选端口现场复核', r.returncode == 0 and not listeners, listeners)
query = "@(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like '*\\.cache\\t22-runtime\\bin\\*' } | Select-Object ProcessId) | ConvertTo-Json -Compress"
r = subprocess.run(['powershell', '-NoProfile', '-Command', query], capture_output=True, text=True)
owned = json.loads(r.stdout) if r.stdout.strip() else []
check('缓存二进制进程现场复核', r.returncode == 0 and not owned, owned)
check('本轮最终stop_all退出记录', read(PROFILE / 'final_stop_all.json')['exit_code'] == 0)

if prefix.startswith('16_'):
    check('原始任务书指纹', sha(PROJECT / 'T22_门诊护栏规则_学生版任务书.docx') == '424200AA2FCDF333FCE984F008E4BEA168D02E3A33B3BECEA00893500E2FB759')
    check('业务源保持40项回归对应版本', sha(STAGE / '实验代码/probe/services.py') == '00D19868C07D8B1470E89342A0B1DE7272795B63B0FBD06AF1E08A661DC642BC')
    for tag, servers in [('batch_client8_server1_360', 1), ('batch_client8_server4_direct_360', 4), ('batch_client8_server4_proxy_360', 4)]:
        data = read(PROFILE / (tag + '_summary.json'))
        samples = data['database_status_samples']
        valid = (data['services_sha256'] == sha(STAGE / '实验代码/probe/services.py')
                 and data['load_processes'] == 8 and len(data['server_pids']) == servers
                 and data['server_processes'] == servers and data['server_threads_total'] == 32
                 and not data['database_monitor_errors'] and samples
                 and all(row['Connection_errors_max_connections'] == 0 for row in samples))
        check('本轮配置与连接观察: ' + tag, valid,
              {'sampled_connected_max': max(row['Threads_connected'] for row in samples),
               'cumulative_max_used': max(row['Max_used_connections'] for row in samples),
               'scope': 'sampled observations include one observer; total threads recorded by experiment configuration'})
    contract = read(PROFILE / 'proxy_contract_v2.json')
    check('统一入口16项实际契约证据', contract['passed'] and len(contract['checks']) == 16
          and all(row['passed'] for row in contract['checks']) and contract['exception_class'] is None
          and len(set(contract['same_doctor_read_pids'])) == 4
          and contract['same_doctor_read_pids'] == contract['expected_backend_pids'])
    prepared = read(PROFILE / '统一入口/preparation-checks.json')
    check('统一入口配置核验', prepared['config_assertions_passed']
          and prepared['validate_exit'] == prepared['adapt_exit'] == prepared['version_exit'] == 0
          and sha(RUNTIME / 'config/caddy/Caddyfile').lower() == prepared['config_sha256'])
    check('本轮最终关闭退出记录', read(PROFILE / 'final_stop_all_four_workers.json')['exit_code'] == 0)

result = {'checked_utc': datetime.now(timezone.utc).isoformat(), 'kind': 'increment_evidence_check_not_acceptance',
          'passed': all(c['passed'] for c in checks), 'checks': checks, 'profiled_metrics': metrics,
          'source_inventory': [{'path': str(p.relative_to(STAGE)), 'sha256': sha(p)} for p in sorted(files)],
          'performance_requirement_met': False, 'performance_result_scope': 'current diagnostic workload only; formal protocol pending', 'formal_AC06_executed': False}
(STAGE / (prefix + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
report = ['# ' + prefix.split('_', 1)[1], '', '> ' + result['checked_utc'] + '；本轮增量检查，不覆盖其他编号检查点，也不重新执行全部数据库测试。', '',
          f"检查 {sum(c['passed'] for c in checks)}/{len(checks)} 项通过。", '', '| 检查 | 结果 |', '| --- | --- |']
report += [f"| {c['check']} | {'通过' if c['passed'] else '失败，见JSON明细'} |" for c in checks]
conclusion = ('业务源保持已通过40项的版本，本轮没有重跑40项。统一入口实际16项契约通过；当前持续在途短测P95仍超280ms，正式协议待审、AC-06未执行。单次大集合776ms不等于正式P95失败。' if prefix.startswith('16_') else '当前业务切片完整回归40项通过；双入口诊断及大集合完整HTTP仍未满足280ms，正式AC-06未执行。')
report += ['', f'详细统计、计时字段覆盖、360在途请求核对和当前源指纹见[同名JSON]({prefix}.json)。', '',
           conclusion + '检查通过仅说明上述证据一致性与当前停机状态，不能当产品/性能验收通过。', '']
(STAGE / (prefix + '.md')).write_text('\n'.join(report), encoding='utf-8')
print(json.dumps({'passed': result['passed'], 'checks': len(checks), 'failed': [c['check'] for c in checks if not c['passed']]}, ensure_ascii=False))
sys.exit(0 if result['passed'] else 1)
