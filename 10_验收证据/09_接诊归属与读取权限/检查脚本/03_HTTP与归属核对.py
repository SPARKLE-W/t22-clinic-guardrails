import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

stage = Path(__file__).resolve().parent.parent
output = stage / '原始结果/http_clinic_01.json'
if output.exists():
    raise RuntimeError('Refuse to overwrite an evidence label')
private = Path.home() / '.cache/t22-runtime/private'
credentials = json.loads((private / 'app-demo-accounts.json').read_text(encoding='utf-8'))
checks = []


def check(name, passed, details=None):
    checks.append({'check': name, 'passed': bool(passed), 'details': details})


def call(browser, path, body=None, key=None):
    opener, jar = browser
    headers, data = {}, None
    if body is not None:
        data = json.dumps(body).encode()
        headers = {'Content-Type': 'application/json', 'X-CSRFToken': next(c.value for c in jar if c.name == 'csrftoken'), 'Idempotency-Key': key or str(uuid.uuid4())}
    try:
        response = opener.open(urllib.request.Request('http://127.0.0.1:18730'+path, data=data, headers=headers), timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    raw = response.read()
    return response.code, json.loads(raw) if response.headers.get('Content-Type', '').startswith('application/json') else None


def login(username):
    jar = http.cookiejar.CookieJar()
    browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar
    call(browser, '/')
    account = next(r for r in credentials if r['username'] == username)
    status, _ = call(browser, '/api/login', {k: account[k] for k in ['username', 'password']})
    if status != 200:
        raise RuntimeError('Synthetic login failed')
    return browser


sys.path.insert(0, str(stage.parent.parent / '05_系统代码'))
os.environ.update(T22_APP_CONFIG=str(private / 'app.json'), DJANGO_SETTINGS_MODULE='vetapp.settings')
import django
django.setup()
from clinic.models import AuditTarget, ClinicalRecord, Encounter, PetIdentity, ReceptionRevision
from governance.models import AuditEvent, Operation

front = login('a_frontdesk')
status, listed = call(front, '/api/receptions')
row = next(r for r in listed['items'] if r['pet_name'] == '读取权限演示宠物')
ref = row['encounter_ref']
check('实际浏览器建档已持久保存', status == 200 and Encounter.objects.filter(pk=ref).exists(), {'encounter_ref': ref})
operation = Operation.objects.filter(actor__username='a_frontdesk', permission='encounter.create').latest('id')
payload = {k: row[k] for k in ['pet_ref', 'species', 'complaint', 'doctor_ref', 'pet_name']}
payload['pet_identity_choice'] = 'new'
before = Encounter.objects.count()
status, replay = call(front, '/api/encounters', payload, str(operation.key))
check('真实HTTP同一建档操作重试仅一笔', status == 200 and replay['encounter_ref'] == ref and Encounter.objects.count() == before)
status, _ = call(front, '/api/encounters/'+ref+'/corrections', {'doctor_ref': row['doctor_ref'], 'original_version_ref': row['reception_versions'][-1]['version_ref'], 'reason': '隐藏字段拒绝'})
check('HTTP不能通过更正改写接诊归属', status == 422)
status, corrected = call(front, '/api/encounters/'+ref+'/corrections', {'complaint': 'HTTP合成主诉更正，原主诉保留', 'original_version_ref': row['reception_versions'][-1]['version_ref'], 'reason': '自动化纠错（合成）'})
check('HTTP更正保留两版资料', status == 200 and len(corrected['reception_versions']) == 2 and corrected['reception_versions'][0]['complaint'] == row['complaint'])
status, marked = call(front, '/api/encounters/'+ref+'/misregistration', {'reason': '自动化误建验证（合成，不删除）'})
check('HTTP误建标记保留旧单', status == 200 and marked['error_mark'] and Encounter.objects.filter(pk=ref).exists())
status, check_pet = call(front, '/api/encounters/create-options/pet-reference-check', {'pet_ref': row['pet_ref']})
check('辅助核验不返回历史或身份正文', status == 200 and check_pet['associable'] and set(check_pet) == {'pet_ref', 'associable'})
status, _ = call(front, '/api/receptions?fields=diagnosis')
check('前台查询临床字段拒绝', status == 422)
doctor = login('a_doctor')
status, detail = call(doctor, '/api/clinical/encounters/'+ref)
check('医生可读其负责的误建接诊', status == 200 and detail['reception']['error_mark'])
check('尚无正式病历不冒充空诊断', detail['clinical'] is None and detail['reception']['complaint'] == corrected['complaint'])
for name in ['a_manager', 'a_frontdesk', 'b_doctor']:
    account = login(name)
    status, denied = call(account, '/api/clinical/encounters/'+ref)
    check(name+'直调临床接口拒绝', status == 403 and 'clinical' not in denied)
event = AuditEvent.objects.filter(actor__username='b_doctor', target=ref, normalized_reason='TARGET_UNAVAILABLE').latest('created_at')
check('跨店拒绝只归请求账号B店', str(event.store_id) == next(r['store_id'] for r in credentials if r['username']=='b_doctor'))
manager_b = login('b_manager')
status, audits = call(manager_b, '/api/audit?operator_ref='+str(event.actor_id))
check('B店管理只可见获准拒绝元数据', status == 200 and any(r['event_ref'] == str(event.pk) for r in audits['items']) and '主诉' not in json.dumps(audits, ensure_ascii=False))
check('实际读取留逐接诊审计引用', AuditTarget.objects.filter(target=ref, kind='encounter', event__actor__username='a_doctor', event__action='clinical.read').exists())
stored = Encounter.objects.get(pk=ref)
check('更正及误建没有改写实际归属', stored.receptionist_id == operation.actor_id and stored.doctor_id == row['doctor_ref'])
check('应用未通过夹具伪造正式病历', ClinicalRecord.objects.count() == 0)
check('接待历史和宠物最小登记保留', ReceptionRevision.objects.filter(encounter=stored).count() == 2 and PetIdentity.objects.filter(pk=stored.pet_id).exists())
report = {'passed': all(r['passed'] for r in checks), 'checks': checks, 'scope': 'synthetic_reception_real_browser_and_HTTP_not_clinical_write_acceptance', 'encounter_ref': ref}
output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'passed': report['passed'], 'checks': len(checks), 'failed': [r['check'] for r in checks if not r['passed']]}))
raise SystemExit(0 if report['passed'] else 1)
