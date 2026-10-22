import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

stage = Path(__file__).resolve().parents[1]
output = stage / '原始结果/http_01.json'
if output.exists():
    raise RuntimeError('Refuse to overwrite evidence')
private = Path.home() / '.cache/t22-runtime/private'
accounts = json.loads((private / 'app-demo-accounts.json').read_text(encoding='utf-8'))


def call(browser, path, body=None, key=None):
    opener, jar = browser
    headers, data = {}, None
    if body is not None:
        data = json.dumps(body).encode()
        headers = {'Content-Type': 'application/json', 'X-CSRFToken': next(c.value for c in jar if c.name == 'csrftoken'),
                   'Idempotency-Key': key or str(uuid.uuid4())}
    try:
        response = opener.open(urllib.request.Request('http://127.0.0.1:18730' + path, data=data, headers=headers), timeout=20)
    except urllib.error.HTTPError as error:
        response = error
    raw = response.read()
    return response.code, json.loads(raw) if response.headers.get('Content-Type', '').startswith('application/json') else None


def login(name):
    jar = http.cookiejar.CookieJar()
    browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar
    call(browser, '/')
    account = next(row for row in accounts if row['username'] == name)
    if call(browser, '/api/login', {key: account[key] for key in ['username', 'password']})[0] != 200:
        raise RuntimeError('Synthetic account login failed')
    return browser


sys.path.insert(0, str(stage.parents[1] / '05_系统代码'))
os.environ.update(T22_APP_CONFIG=str(private / 'app.json'), DJANGO_SETTINGS_MODULE='vetapp.settings')
import django
django.setup()
from clinic.models import Alert, AlertOutbox, ClinicalCheck, ClinicalRecord, ClinicalRevision, GuardHit
from governance.models import AuditEvent

checks = []


def check(name, value):
    checks.append({'check': name, 'passed': bool(value)})
    if not value:
        raise AssertionError(name)


front, doctor, foreign, manager = [login(name) for name in ['a_frontdesk', 'a_doctor', 'b_doctor', 'a_manager']]
_, options = call(front, '/api/encounters/create-options/doctors')
assigned = next(row['doctor_ref'] for row in options['doctor_options'] if row['display_name'] == 'a_doctor')
status, row = call(front, '/api/encounters', {'pet_identity_choice': 'new', 'pet_ref': str(uuid.uuid4()), 'species': '猫',
                                           'complaint': '仅合成演示，无真实临床用途', 'doctor_ref': assigned, 'pet_name': '临床写入演示宠物'})
check('真实HTTP前台创建合成接诊', status == 200)
ref = row['encounter_ref']
path = '/api/clinical/encounters/' + ref + '/diagnosis'
key = str(uuid.uuid4())
status, blocked = call(doctor, path, {'examination': '合成检查'}, key)
check('缺诊断实际SR02拦截且不写正式病历', status == 422 and blocked['error']['code'] == 'GUARD_BLOCKED' and not ClinicalRecord.objects.filter(encounter_id=ref).exists())
check('阻断告警及待发同事务持久', GuardHit.objects.filter(evaluation__encounter_id=ref).count() == 1 and AlertOutbox.objects.filter(alert__hit__evaluation__encounter_id=ref, state='pending').count() == 1)
check('真实同次请求重试不新增告警', call(doctor, path, {'examination': '合成检查'}, key)[1] == blocked and Alert.objects.filter(hit__evaluation__encounter_id=ref).count() == 1)
check('独立提交分别保留告警', call(doctor, path, {'examination': '合成检查'})[0] == 422 and Alert.objects.filter(hit__evaluation__encounter_id=ref).count() == 2)
key = str(uuid.uuid4())
values = {'diagnosis': '合成初版诊断', 'examination': '合成检查'}
status, saved = call(doctor, path, values, key)
check('接诊医生真实写入正式病历', status == 200 and ClinicalRevision.objects.filter(record__encounter_id=ref).count() == 1)
check('成功重试不新增病历版本', call(doctor, path, values, key)[1] == saved and ClinicalRevision.objects.filter(record__encounter_id=ref).count() == 1)
check('前台管理和跨店医生直接API写入拒绝', all(call(actor, path, values)[0] == 403 for actor in [front, manager, foreign]))
event = AuditEvent.objects.filter(actor__username='b_doctor', action='clinical.diagnosis.write').latest('created_at')
check('跨店写拒绝只归请求账号门店', str(event.store_id) != row['store_ref'] and str(event.target) == ref)
before = Alert.objects.count()
check('格式错误不冒充临床告警', call(doctor, path, {'diagnosis': 12})[0] == 422 and Alert.objects.count() == before)
old = saved['clinical_versions'][-1]
correction = {'diagnosis': '合成更正版诊断', 'reason': '合成核对更正', 'original_content_ref': saved['record_ref'], 'original_version_ref': old['version_ref']}
status, corrected = call(doctor, path + '/corrections', correction)
check('真实更正保留原值与实际医生', status == 200 and corrected['clinical_versions'][0]['diagnosis'] == '合成初版诊断' and corrected['clinical_versions'][-1]['actor_ref'] == assigned)
check('旧版本重复更正拒绝', call(doctor, path + '/corrections', correction)[0] == 409 and ClinicalRevision.objects.filter(record__encounter_id=ref).count() == 2)
_, listed = call(doctor, '/api/clinical/alerts')
target_alert = next(item for item in listed['items'] if item['event_ref'] == blocked['event_ref'])
check('医生工作台告警未处理且无伪造处理人', target_alert['handling_state'] == 'unhandled' and target_alert['handler_ref'] is None)
check('跨店告警列表不泄漏本店事件', all(item['alert_ref'] != target_alert['alert_ref'] for item in call(foreign, '/api/clinical/alerts')[1]['items']))
check('管理及跨店目标告警读取拒绝', all(call(actor, '/api/clinical/alerts?alert_ref=' + target_alert['alert_ref'])[0] == 403 for actor in [manager, foreign]))
check('原接诊标记误建后不再新增诊疗', call(front, '/api/encounters/' + ref + '/misregistration', {'reason': '合成误建，用于验证已有内容可纠错'})[0] == 200 and call(doctor, path, values)[0] == 409)
status, resolved = call(doctor, '/api/clinical/alerts/' + target_alert['alert_ref'] + '/resolution',
                        {'alert_ref': target_alert['alert_ref'], 'handling_result': '已核对缺项，原阻断记录保留'})
check('误建原告警仍可由原医生处理', status == 200 and resolved['handling_state'] == 'handled' and resolved['handler_ref'] == assigned)
check('业务处理不伪造消息已投递', AlertOutbox.objects.get(alert_id=target_alert['alert_ref']).state == 'pending')
check('每笔校验保留原模板和当前规则依据', ClinicalCheck.objects.filter(encounter_id=ref).count() == 4 and all(row.policy_digest == row.policy.digest for row in ClinicalCheck.objects.filter(encounter_id=ref).select_related('policy')))
report = {'passed': all(row['passed'] for row in checks), 'checks': checks, 'encounter_ref': ref,
          'scope': 'synthetic_loopback_http_not_real_clinical_or_performance_acceptance'}
output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'passed': report['passed'], 'checks': len(checks), 'encounter_ref': ref}))
