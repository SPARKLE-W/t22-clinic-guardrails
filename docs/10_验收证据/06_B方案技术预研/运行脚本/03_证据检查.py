"""Read-only evidence verification; writes only this stage's check report/JSON."""
import ast
import hashlib
import importlib.metadata
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
checks = []


def check(label, passed, details=None):
    checks.append({'check': label, 'passed': bool(passed), 'details': details})


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


baselines = {
    '03_需求规格说明书.md': 'FE95C5BF2495B763EA2C7075C46CBE5D367F7F82401E2E1818EF81BF9243DAE8',
    '03_需求规格说明书.docx': '844778341B7EB43907FF43CB08AD6B76DB6A9ACE2C77D9B02A028C895958B011',
    '10_验收证据/05_需求基线与首轮设计/02_需规v1.0_基线快照.md': 'FE95C5BF2495B763EA2C7075C46CBE5D367F7F82401E2E1818EF81BF9243DAE8',
}
for name, expected in baselines.items():
    check('冻结基线指纹: ' + name, sha(PROJECT / name) == expected, {'sha256': sha(PROJECT / name)})

json_files = sorted(STAGE.rglob('*.json'))
invalid_json = []
for path in json_files:
    try:
        read_json(path)
    except (ValueError, UnicodeError):
        invalid_json.append(str(path.relative_to(STAGE)))
check('本阶段JSON可解析', not invalid_json, {'files': len(json_files), 'invalid': invalid_json})

metrics = []
for summary_path in sorted(RAW.glob('*_summary.json')):
    data = read_json(summary_path)
    samples_path = summary_path.with_name(summary_path.name.replace('_summary.json', '_samples.jsonl'))
    if not samples_path.exists() or 'groups' not in data:
        continue
    samples = [json.loads(line) for line in samples_path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    failures = sum(not row['ok'] for row in samples)
    valid = data['samples'] == len(samples) and data['failures'] == failures
    group_result = {}
    for key, expected in data['groups'].items():
        rows = [row for row in samples if row['category'] == key]
        latencies = sorted(row['ms'] for row in rows)
        p95 = latencies[max(0, math.ceil(0.95 * len(rows)) - 1)]
        group_ok = (len(rows) == expected['samples'] and sum(not row['ok'] for row in rows) == expected['failures']
                    and math.isclose(p95, expected['p95_all_ms'], abs_tol=1e-7))
        valid = valid and group_ok
        group_result[key] = {'samples': len(rows), 'p95_all_ms': p95}
    throughput = (len(samples) - failures) * 60 / data['wall_including_drain_seconds']
    valid = valid and math.isclose(throughput, data['successful_per_minute'], abs_tol=1e-7)
    events = sorted([(row['started_s'], 1) for row in samples] + [(row['ended_s'], -1) for row in samples])
    in_flight = peak = 0
    for _, change in events:
        in_flight += change
        peak = max(peak, in_flight)
    row = {'summary': summary_path.name, 'samples': len(samples), 'failures': failures,
           'workers_with_samples': len({s['worker'] for s in samples}), 'peak_client_inflight': peak,
           'successful_per_minute': throughput, 'groups': group_result}
    metrics.append(row)
    check('逐样本复算: ' + summary_path.stem, valid, row)

test_record = read_json(RAW / 'pre01_final_race_green.json')
test_log = (RAW / 'pre01_final_race_green.txt').read_text(encoding='utf-8-sig')
check('最后完整实验测试原始记录', test_record['exit_code'] == 0 and 'Ran 37 tests in 70.259s' in test_log
      and re.search(r'^OK\s*$', test_log, re.M), '归档37项通过；本检查不重新执行数据库测试')
mq_full = (RAW / 'mq_green_01.txt').read_text(encoding='utf-8-sig')
mq_later = (RAW / 'mq_green_02_stronger_alert_scope.txt').read_text(encoding='utf-8-sig')
check('MQ完整及后续跳过分开记录', 'Ran 9 tests' in mq_full and re.search(r'^OK\s*$', mq_full, re.M)
      and 'OK (skipped=2)' in mq_later
      and sha(STAGE / '实验代码/mq_probe.py') == '4DB387D16CC0320640A30E5E1B76BEA472AD3EB8EC3A7674321CD9C955BA372F')
response_loss = read_json(RAW / 'http_commit_response_loss.json')
check('真实HTTP已提交响应丢失归档', response_loss['backend_response_observed'] and response_loss['retry_reused_original_result']
      and response_loss['medication_count'] == response_loss['write_audit_count'] == 1)
before, after = [read_json(RAW / name) for name in ['before_restart_db_state.json', 'after_restart_db_state.json']]
check('正常数据库重启前后逐表对账', before['tables'] == after['tables'] and len(before['tables']) == 15
      and sum(t['rows'] for t in before['tables'].values()) == 113754)

source_files = sorted(p for p in (STAGE / '实验代码').rglob('*') if p.is_file()
                      and '__pycache__' not in p.parts and p.suffix in {'.py', '.in', '.lock'})
syntax_errors = []
for path in source_files:
    if path.suffix == '.py':
        try:
            ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
        except SyntaxError:
            syntax_errors.append(str(path.relative_to(STAGE)))
check('实验Python源语法', not syntax_errors, {'source_files': len(source_files), 'syntax_errors': syntax_errors})
lock_rows = [line.strip().split('==') for line in (STAGE / '实验代码/requirements.lock').read_text().splitlines() if '==' in line]
versions = {name: importlib.metadata.version(name) for name, _ in lock_rows}
check('实际venv版本匹配依赖锁', all(versions[name] == version for name, version in lock_rows), versions)

runtime = Path.home() / '.cache/t22-runtime'
connections = read_json(runtime / 'private/connections.json')
secrets = [connections['mysql']['password'], connections['rabbitmq']['password'],
           (runtime / 'private/django.key').read_text().strip()]
public_files = sorted(p for p in STAGE.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
public_files += list((PROJECT / '04_设计与控制文件').rglob('*.md')) + list((PROJECT / '07_AI使用留痕').glob('*.md'))
secret_hits = []
for path in public_files:
    content = path.read_bytes()
    if any(value and value.encode() in content for value in secrets):
        secret_hits.append(str(path.relative_to(PROJECT)))
check('公开产物未含实际连接密码及Django密钥精确值', not secret_hits,
      {'files': len(public_files), 'matching_files': secret_hits, 'scope': 'known values only; values never emitted'})

markdown_files = list(STAGE.glob('*.md')) + list((PROJECT / '04_设计与控制文件').glob('*.md'))
markdown_files += [PROJECT / '00_作业导航.md', PROJECT / '02_T22完成流程与人机任务分工.md',
                   PROJECT / '07_AI使用留痕/05_B方案预研协作与实际结果.md']
broken_links = []
for path in markdown_files:
    for target in re.findall(r'\]\(([^\n)]+)\)', path.read_text(encoding='utf-8-sig')):
        target = unquote(target.strip('<>').split('#')[0])
        if not target or re.match(r'^(https?://|mailto:|codex:)', target):
            continue
        linked = Path(target)
        if not linked.is_absolute():
            linked = path.parent / linked
        if not linked.exists():
            broken_links.append({'source': str(path.relative_to(PROJECT)), 'target': target})
check('当前导航和报告本地链接可达', not broken_links, {'files': len(markdown_files), 'broken': broken_links})

query = "@((Get-NetTCPConnection -State Listen -ErrorAction Stop) | Where-Object { $_.LocalPort -in @(18722,13322,15672,25692,14369) } | Select-Object LocalAddress,LocalPort,OwningProcess) | ConvertTo-Json -Compress"
ports = subprocess.run(['powershell', '-NoProfile', '-Command', query], capture_output=True, text=True)
listeners = json.loads(ports.stdout) if ports.stdout.strip() else []
stop = read_json(RAW / '环境/final-stop.json')
check('最终任务端口现场复核', ports.returncode == 0 and not listeners, {'ports': [18722,13322,15672,25692,14369], 'listeners': listeners, 'query_exit': ports.returncode})
check('运行环境最终进程关闭归档', stop['stop_all']['exit_code'] == 0 and stop['all_task_processes_gone'] and stop['all_task_ports_clear'])

result = {'checked_utc': datetime.now(timezone.utc).isoformat(), 'kind': 'evidence_check_not_product_acceptance',
          'passed': all(c['passed'] for c in checks), 'checks': checks,
          'source_inventory': [{'path': str(p.relative_to(STAGE)), 'sha256': sha(p)} for p in source_files],
          'performance_requirement_met': False, 'formal_AC06_executed': False}
(STAGE / '08_预研证据与基线检查.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
report = ['# 预研证据与基线检查', '', '> ' + result['checked_utc'] + '；检查证据一致性，不代替产品验收或重新运行全部测试。', '',
          f"检查：{sum(c['passed'] for c in checks)}/{len(checks)}项通过。", '', '| 检查 | 结果 |', '| --- | --- |']
report += [f"| {c['check']} | {'通过' if c['passed'] else '失败，见JSON明细'} |" for c in checks]
report += ['', '详细值、8组负载逐样本复算、峰值在途客户端数及实验源文件SHA256见[同名JSON](08_预研证据与基线检查.json)。', '',
           '需规Markdown、冻结快照及Word指纹均按定稿记录核对；当前端口检查覆盖HTTP/MySQL/AMQP/distribution/EPMD。已知秘密精确扫描不会证明任意敏感内容都不存在。', '',
           '**性能仍未达标，正式AC-06未执行。** 本文件检查通过只支持文件、统计及运行状态证据的可信范围；完整系统、控制编译器、正式10分钟负载、覆盖率和工具验收均不能据此认定完成。', '']
(STAGE / '08_预研证据与基线检查.md').write_text('\n'.join(report), encoding='utf-8')
print(json.dumps({'passed': result['passed'], 'checks': len(checks), 'failed': [c['check'] for c in checks if not c['passed']]}, ensure_ascii=False))
sys.exit(0 if result['passed'] else 1)
