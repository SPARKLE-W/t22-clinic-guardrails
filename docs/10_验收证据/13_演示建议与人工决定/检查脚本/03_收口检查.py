import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path
from jsonschema import Draft202012Validator

stage=Path(__file__).resolve().parents[1]
project=stage.parents[2]
checks=[]
def check(name,value):
    checks.append({'check':name,'passed':bool(value)})

for name,digest in {
    '03_需求规格说明书.md':'FE95C5BF2495B763EA2C7075C46CBE5D367F7F82401E2E1818EF81BF9243DAE8',
    '03_需求规格说明书.docx':'844778341B7EB43907FF43CB08AD6B76DB6A9ACE2C77D9B02A028C895958B011',
}.items():
    check(name,hashlib.sha256((project/name).read_bytes()).hexdigest().upper()==digest)
full=json.loads((stage/'原始结果/full_01.json').read_text(encoding='utf-8'))
log=(stage/'原始结果/full_01.txt').read_text(encoding='utf-8')
check('完整107项回归退出0',full['exit_code']==0 and 'Ran 107 tests' in log and '\nOK\n' in log)
http=json.loads((stage/'原始结果/http_02.json').read_text(encoding='utf-8'))
check('实际HTTP21项全部通过',http['passed'] and http['count']==21 and all(row['passed'] for row in http['checks']))
files=list((project/'backend').rglob('*.py'))
for path in files:
    ast.parse(path.read_text(encoding='utf-8'),filename=str(path))
check('Python及迁移语法',True)
spec=json.loads((project/'04_设计与控制文件/控制文件/03_建议接口_openapi_v0.1.json').read_text(encoding='utf-8'))
for schema in spec['components']['schemas'].values():
    Draft202012Validator.check_schema(schema)
routes=(project/'backend/vetapp/urls.py').read_text(encoding='utf-8')
registered={'/'+re.sub(r'<uuid:([^>]+)>',r'{\1}',route) for route in re.findall(r"path\('([^']*)'",routes)}
check('OpenAPI三路由实际注册',len(spec['paths'])==3 and set(spec['paths'])<=registered)
paths=subprocess.check_output(['git','diff','--name-only','HEAD'],cwd=project).decode('utf-8')
check('本批未修改前端模板',not any('templates/' in path.replace('\\','/') for path in paths.splitlines()))
for name in ['13_前端对接与接口交接.md','14_演示建议与人工决定接口.md']:
    path=project/'04_设计与控制文件'/name
    links=re.findall(r'\]\(([^)]+)\)',path.read_text(encoding='utf-8'))
    check(name+'链接',all((path.parent/link).exists() for link in links if not link.startswith(('http','#'))))
report={'passed':all(row['passed'] for row in checks),'count':len(checks),'checks':checks}
(stage/'04_检查结果.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'passed':report['passed'],'count':len(checks)}))
raise SystemExit(0 if report['passed'] else 1)
