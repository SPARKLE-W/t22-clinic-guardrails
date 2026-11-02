"""Real loopback HTTP with CSRF/session; all test clinical text is synthetic."""
import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

stage=Path(__file__).resolve().parents[1]
project=stage.parents[2]
output=stage/'原始结果/http_02.json'
if output.exists():
    raise RuntimeError('Refuse to overwrite evidence')
private=Path.home()/'.cache/t22-runtime/private'
accounts=json.loads((private/'app-demo-accounts.json').read_text(encoding='utf-8'))
spec=json.loads((project/'04_设计与控制文件/控制文件/03_建议接口_openapi_v0.1.json').read_text(encoding='utf-8'))
registry=Registry().with_resource('urn:t22:suggestions',Resource.from_contents({'$schema':'https://json-schema.org/draft/2020-12/schema',**spec}))


def valid_schema(name,value):
    validator=Draft202012Validator({'$ref':'urn:t22:suggestions#/components/schemas/'+name},registry=registry,format_checker=FormatChecker())
    return not list(validator.iter_errors(value))


def call(browser,path,body=None,key=None,csrf=True):
    opener,jar=browser
    headers,data={},None
    if body is not None:
        data=json.dumps(body).encode()
        headers={'Content-Type':'application/json','Idempotency-Key':key or str(uuid.uuid4())}
        if csrf:
            headers['X-CSRFToken']=next(c.value for c in jar if c.name=='csrftoken')
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


front=login('a_frontdesk');doctor=login('a_doctor')
_,options=call(front,'/api/encounters/create-options/doctors')
doctor_ref=next(row['doctor_ref'] for row in options['doctor_options'] if row['display_name']=='a_doctor')
status,encounter=call(front,'/api/encounters',{'pet_identity_choice':'new','pet_ref':str(uuid.uuid4()),'species':'合成演示宠物',
    'complaint':'建议HTTP闭环合成主诉','doctor_ref':doctor_ref})
check('前台实际创建独立合成接诊',status==200)
ref=encounter['encounter_ref'];base='/api/clinical/encounters/'+ref
command=[sys.executable,'manage.py','seed_demo_suggestions','--encounter-ref',ref]
result=subprocess.run(command,cwd=project/'backend',env={**os.environ,'T22_APP_CONFIG':str(private/'app.json'),
    'PYTHONIOENCODING':'utf-8','PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True,encoding='utf-8')
check('受控命令初始化演示建议',result.returncode==0)
status,listed=call(doctor,base+'/suggestions')
check('医生获取两条明确标注演示来源',status==200 and len(listed['items'])==2 and all(row['source']['type']=='demo' and '合成演示' in row['source']['label'] for row in listed['items']))
check('实际读取响应符合本批OpenAPI结构',valid_schema('List',listed))
status,before=call(doctor,base)
check('生成建议不自动写入正式病历',status==200 and before['clinical'] is None)
item,other=listed['items'];path='/api/clinical/suggestions/'+item['suggestion_ref']
values={'encounter_ref':ref,'suggestion_ref':item['suggestion_ref'],'adopted_content':{'diagnosis':'HTTP核对后的合成诊断'}}
status,blocked=call(doctor,path+'/adoption',values)
check('缺采纳理由被运行护栏阻断',status==422 and blocked['error']['code']=='GUARD_BLOCKED')
check('实际护栏拒绝响应符合本批OpenAPI结构',valid_schema('Error',blocked))
check('被阻断后仍无正式病历',call(doctor,base)[1]['clinical'] is None)
key=str(uuid.uuid4());values['reason']='合成演示决定，不代表真实医生临床认可'
check('直接调用API也要求CSRF',call(doctor,path+'/adoption',values,key,csrf=False)[0]==403)
status,saved=call(doctor,path+'/adoption',values,key)
check('带理由实际采纳入库',status==200 and saved['operation_state']=='adopted')
check('实际采纳请求及响应符合本批OpenAPI结构',valid_schema('Adoption',values) and valid_schema('Result',saved))
check('写响应只有引用和真实责任',saved['responsibility_metadata']['actor_ref']==doctor_ref and 'adopted_content' not in saved and 'diagnosis' not in saved)
check('同一请求重试返回同一决定',call(doctor,path+'/adoption',values,key)[1]==saved)
check('同键换理由明确冲突',call(doctor,path+'/adoption',{**values,'reason':'另一合成理由'},key)[0]==409)
status,after=call(doctor,base)
check('临床实际读取采纳内容及其来源',status==200 and after['clinical']['diagnosis']=='HTTP核对后的合成诊断' and len(after['clinical']['adoptions'])==1)
status,retained=call(doctor,base+'/suggestions')
adopted=next(row for row in retained['items'] if row['suggestion_ref']==item['suggestion_ref'])
check('原始建议不被医生编辑覆盖',adopted['content']==item['content'] and adopted['source']==item['source'] and adopted['decision']['reason']==values['reason'])
decline={'encounter_ref':ref,'suggestion_ref':other['suggestion_ref'],'decision':'declined'}
status,declined=call(doctor,'/api/clinical/suggestions/'+other['suggestion_ref']+'/decline',decline)
check('未采纳无理由仍保留决定',status==200 and declined['operation_state']=='declined')
check('实际不采纳请求及响应符合本批OpenAPI结构',valid_schema('Decline',decline) and valid_schema('Result',declined))
check('未采纳不增加正式病历采纳条目',len(call(doctor,base)[1]['clinical']['adoptions'])==1)
check('已采纳不能改报未采纳',call(doctor,path+'/decline',{'encounter_ref':ref,'suggestion_ref':item['suggestion_ref'],'decision':'declined'})[0]==409)
check('跨角色及跨店直接API均拒绝',all(call(login(name),base+'/suggestions')[0]==403 and call(login(name),path+'/adoption',values)[0]==403 for name in ['a_frontdesk','a_manager','b_doctor']))
report={'passed':all(row['passed'] for row in checks),'checks':checks,'count':len(checks),
        'note':'真实本机HTTP+CSRF；所有账号和临床文字均为合成演示，自动化采纳不等于真实医生认可；无新前端页面'}
output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'passed':report['passed'],'count':len(checks)}))
