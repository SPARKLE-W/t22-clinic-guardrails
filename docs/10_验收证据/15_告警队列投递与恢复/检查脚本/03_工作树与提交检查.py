"""Check this worktree's staged bytes; inspect originals in the primary checkout."""
import hashlib
import json
import re
import subprocess
from pathlib import Path

stage=Path(__file__).resolve().parents[1]
project=stage.parents[2]
def git(*args):
    return subprocess.check_output(['git',*args],cwd=project)

names=git('ls-files','-z').decode('utf-8').split('\0')
names=[name for name in names if name]
private=Path.home()/'.cache/t22-runtime/private'
secrets=set()
def collect(value):
    if isinstance(value,dict):
        for key,item in value.items():
            if key.lower() in {'password','secret_key'} and isinstance(item,str) and item:
                secrets.add(item.encode())
            else:
                collect(item)
    elif isinstance(value,list):
        for item in value:
            collect(item)
for path in private.glob('*.json'):
    collect(json.loads(path.read_text(encoding='utf-8-sig')))
for path in private.glob('*.cnf'):
    for value in re.findall(r'^password\s*=\s*(.+)$',path.read_text(encoding='utf-8'),re.M):
        secrets.add(value.strip().strip('\"\'').encode())
for path in [private/'django.key',private/'erlang-home/.erlang.cookie']:
    if path.exists():
        secrets.add(path.read_bytes().strip())
secrets.discard(b'')
leaks,forbidden,oversized=[],[],[]
for name in names:
    data=git('show',':'+name)
    if any(secret in data for secret in secrets) or re.search(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)',data):
        leaks.append(name)
    if any(part in {'private','__pycache__','.venv','.cache','.worktrees','node_modules'} for part in Path(name).parts) or Path(name).suffix in {'.log','.key','.pem','.db','.pyc'}:
        forbidden.append(name)
    if len(data)>10*1024*1024:
        oversized.append(name)
baseline={
    '03_需求规格说明书.md':'FE95C5BF2495B763EA2C7075C46CBE5D367F7F82401E2E1818EF81BF9243DAE8',
    '03_需求规格说明书.docx':'844778341B7EB43907FF43CB08AD6B76DB6A9ACE2C77D9B02A028C895958B011',
    '10_验收证据/05_需求基线与首轮设计/02_需规v1.0_基线快照.md':'FE95C5BF2495B763EA2C7075C46CBE5D367F7F82401E2E1818EF81BF9243DAE8',
}
frozen=all(hashlib.sha256(git('show',':'+name)).hexdigest().upper()==digest for name,digest in baseline.items())
original=Path(git('rev-parse','--path-format=absolute','--git-common-dir').decode().strip()).parent/'T22_门诊护栏规则_学生版任务书.docx'
source_unchanged=hashlib.sha256(original.read_bytes()).hexdigest().upper()=='424200AA2FCDF333FCE984F008E4BEA168D02E3A33B3BECEA00893500E2FB759'
report={'passed':bool(names) and not (leaks or forbidden or oversized) and frozen and source_unchanged,
        'tracked_files':len(names),'secret_leak_paths':leaks,'forbidden_paths':forbidden,'oversized_paths':oversized,
        'frozen_baseline_preserved':frozen,'primary_task_original_preserved':source_unchanged}
print(json.dumps(report,ensure_ascii=False,indent=2))
raise SystemExit(0 if report['passed'] else 1)
