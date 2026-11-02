import ast
import json
import re
import subprocess
from pathlib import Path
from jsonschema import Draft202012Validator

stage=Path(__file__).resolve().parents[1]
root=stage.parents[1]
checks=[]
def check(name,value):
    checks.append({'check':name,'passed':bool(value)})

full=json.loads((stage/'原始结果/full_final_01.json').read_text(encoding='utf-8'))
log=(stage/'原始结果/full_final_01.txt').read_text(encoding='utf-8')
check('完整132项无跳过',full['exit_code']==0 and 'Ran 132 tests' in log and '\nOK\n' in log and 'skipped=' not in log)
life=json.loads((stage/'原始结果/lifecycle_02.json').read_text(encoding='utf-8'))
check('最终两项真实节点停复',life['passed'] and life['tests']==2 and all(v['exit_code']==0 for v in life['actions']))
red=(stage/'原始结果/review_red_01.txt').read_text(encoding='utf-8')
check('确认持续缺失先有真实失败','blocked beyond the publication deadline' in red and 'FAILED' in red)
for path in (root/'05_系统代码').rglob('*.py'):
    ast.parse(path.read_text(encoding='utf-8'),filename=str(path))
check('Python及迁移语法',True)
schema=json.loads((root/'04_设计与控制文件/控制文件/04_告警传输契约_v1.json').read_text(encoding='utf-8'))
Draft202012Validator.check_schema(schema)
check('内部传输JSON契约语法',True)
for name in ['13_前端对接与接口交接.md','16_告警队列投递与恢复.md']:
    path=root/'04_设计与控制文件'/name
    links=re.findall(r'\]\(([^)]+)\)',path.read_text(encoding='utf-8'))
    check(name+'本地链接',all((path.parent/link).exists() for link in links if not link.startswith(('http','#'))))
changed=subprocess.check_output(['git','diff','--name-only','HEAD'],cwd=root,encoding='utf-8')
check('未改前端与HTTP入口',not any(value in changed for value in ['templates/','frontend/','governance/views.py','vetapp/urls.py']))
report={'passed':all(value['passed'] for value in checks),'count':len(checks),'checks':checks}
(stage/'05_检查结果.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'passed':report['passed'],'count':len(checks)}))
raise SystemExit(0 if report['passed'] else 1)
