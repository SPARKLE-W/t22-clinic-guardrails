"""Read current synthetic browser run and test direct HTTP role boundaries."""
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

stage = Path(__file__).resolve().parent.parent
label = sys.argv[1] if len(sys.argv) > 1 else 'http_checks_01'
output = stage / ('原始结果/' + label + '.json')
if output.exists():
    raise RuntimeError('Refuse to overwrite an evidence label')
private = Path.home() / '.cache/t22-runtime/private'
credentials = json.loads((private / 'app-demo-accounts.json').read_text(encoding='utf-8'))
base = 'http://127.0.0.1:18730'
checks = []


def call(opener, cookies, path, payload=None, csrf=True):
    headers = {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers = {'Content-Type': 'application/json', 'Idempotency-Key': str(uuid.uuid4())}
        if csrf:
            headers['X-CSRFToken'] = next(cookie.value for cookie in cookies if cookie.name == 'csrftoken')
    request = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    raw = response.read()
    result = json.loads(raw) if response.headers.get('Content-Type', '').startswith('application/json') else None
    return response.code, result


def browser(username):
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    call(opener, cookies, '/')
    account = next(row for row in credentials if row['username'] == username)
    status, _ = call(opener, cookies, '/api/login', {key: account[key] for key in ['username', 'password']})
    if status != 200:
        raise RuntimeError('Synthetic account login failed')
    return opener, cookies


def check(name, passed, details=None):
    checks.append({'check': name, 'passed': bool(passed), 'details': details})


opener, cookies = browser('a_manager')
code, result = call(opener, cookies, '/api/candidates')
ref = result['active_candidate_ref']
check('浏览器流程后本店已启用第一版', code == 200 and result['generation'] == 1 and ref is not None, {'candidate_ref': ref, 'generation': result['generation']})
code, detail = call(opener, cookies, '/api/candidates/' + ref)
check('持久候选已批准且生效', code == 200 and detail['state'] == 'approved' and detail['is_active'])
check('自动化演示操作明确标注非真人审阅', '非真人审批' in detail['governance_history']['review']['reason'])
code, audit = call(opener, cookies, '/api/audit')
expected = {'event_ref', 'event_at', 'event_store_ref', 'operator_ref', 'operator_role', 'action', 'outcome', 'normalized_reason', 'target_ref'}
check('真实HTTP审计只含获准元数据', code == 200 and all(set(row) == expected for row in audit['items']) and 'AI自动化' not in json.dumps(audit, ensure_ascii=False))
code, _ = call(opener, cookies, '/api/candidates/activate', {'candidate_ref': ref, 'expected_generation': 1, 'reason': '无CSRF拒绝'}, csrf=False)
check('真实HTTP无CSRF启用拒绝', code == 403)
other, other_cookies = browser('b_manager')
code, values = call(other, other_cookies, '/api/candidates')
check('B店列表不含A店候选', code == 200 and values['items'] == [] and values['generation'] == 0)
code, values = call(other, other_cookies, '/api/candidates/' + ref)
check('B店直接读取A店候选拒绝', code == 403 and values['error']['code'] == 'TARGET_UNAVAILABLE')
for name in ['a_doctor', 'a_frontdesk']:
    restricted, jar = browser(name)
    code, value = call(restricted, jar, '/api/candidates')
    check(name + '直接治理接口拒绝', code == 403 and value['error']['code'] == 'PERMISSION_DENIED')
sys.path.insert(0, str(stage.parent.parent / '05_系统代码'))
os.environ['T22_APP_CONFIG'] = str(private / 'app.json')
os.environ['DJANGO_SETTINGS_MODULE'] = 'vetapp.settings'
import django
django.setup()
from governance.models import Account, Activation, AuditEvent, Candidate, Operation, PolicyHead, Review
from vetcontrols import verify_candidate
candidate = Candidate.objects.get(pk=ref)
check('数据库候选保持静态包原貌及当前底线', verify_candidate(candidate.envelope)['ok'] and candidate.envelope['governance']['state'] == 'draft_unsubmitted')
check('责任账号/审批摘要/启用摘要相符', candidate.submitted_by_id == Review.objects.get(candidate=candidate).actor_id == Activation.objects.get(candidate=candidate).actor_id and candidate.digest == Review.objects.get(candidate=candidate).digest == Activation.objects.get(candidate=candidate).digest)
check('演示账号口令只存哈希', all(Account.objects.get(username=row['username']).check_password(row['password']) and Account.objects.get(username=row['username']).password != row['password'] for row in credentials))
check('跨店治理拒绝按U留存不擅自归店', AuditEvent.objects.filter(actor__username='b_manager', normalized_reason='TARGET_UNAVAILABLE', store__isnull=True).exists())
check('治理四次写入各有幂等操作结果', Operation.objects.filter(actor__username='a_manager').count() == 4)
report = {'label': label, 'checks': checks, 'passed': all(item['passed'] for item in checks),
          'scope': 'synthetic_accounts_browser_run_and_real_HTTP_not_human_approval', 'counts': {
              'accounts': Account.objects.count(), 'candidates': Candidate.objects.count(), 'reviews': Review.objects.count(), 'activations': Activation.objects.count()},
          'active_candidate': str(PolicyHead.objects.get(store=candidate.store).candidate_id)}
output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'passed': report['passed'], 'checks': len(checks), 'failed': [row['check'] for row in checks if not row['passed']]}))
raise SystemExit(0 if report['passed'] else 1)
