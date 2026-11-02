import http.cookiejar
import json
import urllib.request
import urllib.error
import uuid
from pathlib import Path

stage=Path(__file__).resolve().parents[1]
output=stage/'原始结果/http_01.json'
if output.exists():
    raise RuntimeError('Refuse to overwrite evidence')
accounts=json.loads((Path.home()/'.cache/t22-runtime/private/app-demo-accounts.json').read_text(encoding='utf-8'))

def call(browser,path,body=None,key=None):
    opener,jar=browser
    headers,data={},None
    if body is not None:
        data=json.dumps(body).encode()
        headers={'Content-Type':'application/json','X-CSRFToken':next(c.value for c in jar if c.name=='csrftoken'),'Idempotency-Key':key or str(uuid.uuid4())}
    try:
        response=opener.open(urllib.request.Request('http://127.0.0.1:18730'+path,data=data,headers=headers),timeout=20)
    except urllib.error.HTTPError as error:
        response=error
    raw=response.read()
    return response.code,json.loads(raw) if response.headers.get('Content-Type','').startswith('application/json') else None

def login(name):
    jar=http.cookiejar.CookieJar()
    browser=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)),jar
    call(browser,'/')
    account=next(row for row in accounts if row['username']==name)
    if call(browser,'/api/login',{k:account[k] for k in ['username','password']})[0]!=200:
        raise RuntimeError('Synthetic login failed')
    return browser

checks=[]
def check(name,value):
    checks.append({'check':name,'passed':bool(value)})
    if not value:
        raise AssertionError(name)

doctor=login('a_doctor')
status,listed=call(doctor,'/api/clinical/encounters')
row=next(item for item in listed['items'] if item['clinical'] and not item['reception']['error_mark'])
ref=row['reception']['encounter_ref']
path='/api/clinical/encounters/'+ref+'/medications'
key=str(uuid.uuid4())
values={'drug':'HTTP合成演示药','amount':'1','unit':'演示单位'}
status,saved=call(doctor,path,values,key)
check('实际医生追加用药',status==200)
check('写入响应不返回药品正文','drug' not in saved and 'diagnosis' not in saved)
check('成功重试引用一致',call(doctor,path,values,key)[1]==saved)
check('同键不同内容拒绝',call(doctor,path,{**values,'amount':'2'},key)[0]==409)
correct={**values,'amount':'2','original_entry_ref':saved['saved_ref'],'reason':'合成核对更正'}
status,changed=call(doctor,path+'/corrections',correct)
check('追加关联更正',status==200 and changed['original_entry_ref']==saved['saved_ref'])
_,detail=call(doctor,'/api/clinical/encounters/'+ref)
chain=detail['clinical']['medication_chain']
original=next(item for item in chain if item['entry_ref']==saved['saved_ref'])
correction=next(item for item in chain if item['entry_ref']==changed['saved_ref'])
check('原项不变且更正留真实责任',original['amount']=='1' and correction['amount']=='2' and bool(correction['actor_ref']) and bool(correction['created_at']))
check('缺更正理由被护栏阻断',call(doctor,path+'/corrections',{**values,'original_entry_ref':saved['saved_ref']})[0]==422)
check('用药三字段不齐被护栏阻断',call(doctor,path,{'drug':'合成缺项'})[0]==422)
check('其他角色和跨店医生不能写',all(call(login(name),path,values)[0]==403 for name in ['a_frontdesk','a_manager','b_doctor']))
check('读取保留原模板',detail['clinical']['template_version_ref']==row['clinical']['template_version_ref'])
output.write_text(json.dumps({'passed':all(item['passed'] for item in checks),'checks':checks,'encounter_ref':ref,'saved_ref':saved['saved_ref'],'synthetic_only':True},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'passed':True,'checks':len(checks)}))
