import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

stage=Path(__file__).resolve().parents[1]
root=stage.parents[2]
output=stage/'04_检查结果.json'
if output.exists():
    raise RuntimeError('Refuse overwrite')
checks={}
for name,sha in {'03_需求规格说明书.md':'FE95C5BF2495B763EA2C7075C46CBE5D367F7F82401E2E1818EF81BF9243DAE8','03_需求规格说明书.docx':'844778341B7EB43907FF43CB08AD6B76DB6A9ACE2C77D9B02A028C895958B011'}.items():
    checks[name]=hashlib.sha256((root/name).read_bytes()).hexdigest().upper()==sha
checks['full89']='Ran 89 tests' in (stage/'原始结果/full_01.txt').read_text(encoding='utf-8') and json.loads((stage/'原始结果/full_01.json').read_text())['exit_code']==0
checks['http10']=json.loads((stage/'原始结果/http_01.json').read_text(encoding='utf-8'))['passed']
checks['browser']='浏览器合成核对更正' in (stage/'原始结果/browser_01.txt').read_text(encoding='utf-8')
for path in (root/'backend').rglob('*.py'):
    ast.parse(path.read_text(encoding='utf-8'))
checks['python_syntax']=True
for path in (root/'backend').rglob('*.html'):
    for script in re.findall(r'<script>(.*?)</script>',path.read_text(encoding='utf-8'),re.S):
        result=subprocess.run(['node','--check','-'],input=script,text=True,capture_output=True)
        checks[str(path.relative_to(root))]=result.returncode==0
output.write_text(json.dumps({'passed':all(checks.values()),'checks':checks,'subagent_calls':0},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'passed':all(checks.values()),'checks':len(checks)}))
raise SystemExit(0 if all(checks.values()) else 1)
