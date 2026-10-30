import copy
import json
import uuid
import types
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from pathlib import Path
from unittest.mock import patch

from django.db import OperationalError, close_old_connections, connection, transaction
from django.http import JsonResponse
from django.test import Client, TransactionTestCase, override_settings
from django.urls import path

from .models import Account, Activation, AuditEvent, Candidate, Demand, Operation, PolicyHead, Review, Store
from .services import DomainError, detail, mutate, require

GENERATE = 'governance.candidate.generate'
REVIEW = 'governance.candidate.review'
ACTIVATE = 'governance.candidate.activate'


class GovernanceTests(TransactionTestCase):
    def setUp(self):
        self.a = Store.objects.create(id=uuid.UUID('11111111-1111-4111-8111-111111111111'), name='A')
        self.b = Store.objects.create(name='B')
        PolicyHead.objects.bulk_create([PolicyHead(store=self.a), PolicyHead(store=self.b)])
        self.manager = Account.objects.create_user(username='manager-a', password='test-only-password', store=self.a, role='manager')
        self.other = Account.objects.create_user(username='manager-b', password='test-only-password', store=self.b, role='manager')
        self.doctor = Account.objects.create_user(username='doctor-a', password='test-only-password', store=self.a, role='doctor')
        self.front = Account.objects.create_user(username='front-a', password='test-only-password', store=self.a, role='frontdesk')
        self.definition = json.loads((Path(__file__).parents[1] / 'examples/01_完整定义需求_v0.2.json').read_text(encoding='utf-8'))

    def call(self, permission, payload, actor=None, key=None):
        return mutate((actor or self.manager).pk, permission, key or str(uuid.uuid4()), payload)

    def test_frontend_shell_session_handoff_and_scoped_assets(self):
        anonymous = self.client.get('/api/session')
        self.assertEqual(200, anonymous.status_code)
        self.assertEqual({'authenticated': False}, anonymous.json())

        shell = self.client.get('/app')
        self.assertEqual(200, shell.status_code)
        self.assertIn('text/html', shell['Content-Type'])
        self.assertIn('csrftoken', shell.cookies)
        self.assertIn('门诊护栏', b''.join(shell.streaming_content).decode('utf-8'))

        self.client.login(username='manager-a', password='test-only-password')
        info = self.client.get('/api/session').json()
        self.assertEqual({'authenticated', 'username', 'role', 'store_ref', 'store_name'}, set(info))
        self.assertEqual('manager-a', info['username'])
        self.assertEqual('manager', info['role'])
        self.assertEqual(str(self.a.pk), info['store_ref'])
        self.assertEqual('A', info['store_name'])

        script = self.client.get('/app/assets/app.js')
        self.assertEqual(200, script.status_code)
        self.assertIn('javascript', script['Content-Type'])
        self.assertEqual(404, self.client.get('/app/assets/../../vetapp/settings.py').status_code)

        self.client.post('/api/logout')
        self.assertEqual({'authenticated': False}, self.client.get('/api/session').json())

    def generate(self, key=None):
        result = self.call(GENERATE, {'definition': self.definition, 'reason': '定义演示规则'}, key=key)
        self.assertIn('candidate_ref', result)
        return result['candidate_ref']

    def submit(self, ref):
        return self.call(GENERATE, {'candidate_ref': ref, 'reason': '提交人工审阅'})

    def approve(self, ref):
        candidate = Candidate.objects.get(pk=ref)
        return self.call(REVIEW, {'candidate_ref': ref, 'content_digest': candidate.digest, 'decision': 'approve', 'reason': '已查看定义'})

    def ready(self):
        ref = self.generate()
        self.submit(ref)
        self.approve(ref)
        return ref

    def test_lifecycle_requires_explicit_submit_review_and_manual_activation(self):
        ref = self.generate()
        self.assertEqual('draft', Candidate.objects.get(pk=ref).state)
        self.assertIsNone(PolicyHead.objects.get(store=self.a).candidate_id)
        self.submit(ref)
        self.approve(ref)
        self.assertIsNone(PolicyHead.objects.get(store=self.a).candidate_id)
        result = self.call(ACTIVATE, {'candidate_ref': ref, 'expected_generation': 0, 'reason': '手动启用'})
        self.assertEqual(1, result['generation'])
        self.assertEqual(uuid.UUID(ref), PolicyHead.objects.get(store=self.a).candidate_id)
        self.assertEqual(self.manager.pk, Review.objects.get(candidate_id=ref).actor_id)
        self.assertEqual(self.manager.pk, Activation.objects.get(candidate_id=ref).actor_id)

    def test_roles_scope_and_current_account_are_checked_on_every_call(self):
        ref = self.generate()
        for actor in [self.doctor, self.front, self.other]:
            with self.assertRaises(DomainError):
                self.call(GENERATE, {'candidate_ref': ref, 'reason': '越权'}, actor)
        self.manager.is_active = False
        self.manager.save(update_fields=['is_active'])
        with self.assertRaises(DomainError):
            detail(self.manager.pk, ref)
        self.assertEqual('draft', Candidate.objects.get(pk=ref).state)

    def test_unknown_fields_spoofed_actor_and_cross_store_definition_rejected(self):
        for extra in [{'actor_id': self.other.pk}, {'approval': True}, {'store_id': str(self.b.pk)}]:
            with self.assertRaises(DomainError):
                self.call(GENERATE, {'definition': self.definition, 'reason': '提交', **extra})
        changed = copy.deepcopy(self.definition)
        changed['scope']['store_id'] = str(self.b.pk)
        with self.assertRaises(DomainError):
            self.call(GENERATE, {'definition': changed, 'reason': '跨店'})
        self.assertEqual(0, Candidate.objects.count())

    def test_failed_compile_records_safe_report_and_no_candidate(self):
        del self.definition['quality_gates']
        result = self.call(GENERATE, {'definition': self.definition, 'reason': '校验缺项'})
        self.assertFalse(result['validation_results']['ok'])
        self.assertEqual(0, Candidate.objects.count())
        self.assertEqual(1, Demand.objects.count())
        self.assertIsNone(Demand.objects.get().definition)

    def test_review_state_reason_and_digest_are_required(self):
        ref = self.generate()
        candidate = Candidate.objects.get(pk=ref)
        payload = {'candidate_ref': ref, 'content_digest': candidate.digest, 'decision': 'approve', 'reason': '审批'}
        with self.assertRaises(DomainError):
            self.call(REVIEW, payload)
        self.submit(ref)
        for changed in [{**payload, 'content_digest': '0'*64}, {**payload, 'reason': ''}]:
            with self.assertRaises(DomainError):
                self.call(REVIEW, changed)
        self.assertFalse(Review.objects.exists())

    def test_known_own_store_refusal_keeps_target_and_local_audit_scope(self):
        ref = self.generate()
        with self.assertRaises(DomainError):
            self.approve(ref)
        audit = AuditEvent.objects.get(normalized_reason='STATE_OR_VERSION_CONFLICT')
        self.assertEqual(self.a.pk, audit.store_id)
        self.assertEqual(uuid.UUID(ref), audit.target)

    def test_unapproved_rejected_and_modified_candidate_cannot_activate(self):
        ref = self.generate()
        with self.assertRaises(DomainError):
            self.call(ACTIVATE, {'candidate_ref': ref, 'expected_generation': 0, 'reason': '启用'})
        self.submit(ref)
        self.call(REVIEW, {'candidate_ref': ref, 'content_digest': Candidate.objects.get(pk=ref).digest, 'decision': 'reject', 'reason': '驳回'})
        with self.assertRaises(DomainError):
            self.call(ACTIVATE, {'candidate_ref': ref, 'expected_generation': 0, 'reason': '启用'})
        approved = self.ready()
        candidate = Candidate.objects.get(pk=approved)
        candidate.envelope['payload']['demand']['quality_gates']['load']['p95_ms_max'] = 999
        candidate.save(update_fields=['envelope'])
        with self.assertRaises(DomainError):
            self.call(ACTIVATE, {'candidate_ref': approved, 'expected_generation': 0, 'reason': '启用'})
        self.assertEqual(0, Activation.objects.count())

    def test_retry_returns_original_result_and_changed_content_conflicts(self):
        key = str(uuid.uuid4())
        ref = self.generate(key)
        first = self.call(GENERATE, {'definition': self.definition, 'reason': '定义演示规则'}, key=key)
        self.assertEqual(ref, first['candidate_ref'])
        self.assertEqual(1, Candidate.objects.count())
        with self.assertRaises(DomainError):
            self.call(GENERATE, {'definition': self.definition, 'reason': '另一次意图'}, key=key)
        self.manager.role = 'doctor'
        self.manager.save(update_fields=['role'])
        with self.assertRaises(DomainError):
            self.call(GENERATE, {'definition': self.definition, 'reason': '定义演示规则'}, key=key)

    def test_audit_failure_rolls_back_candidate_and_operation(self):
        with patch('governance.services.write_audit', side_effect=RuntimeError('injected audit failure')):
            with self.assertRaises(RuntimeError):
                self.generate()
        self.assertEqual(0, Demand.objects.count())
        self.assertEqual(0, Candidate.objects.count())
        self.assertEqual(0, Operation.objects.count())

    def test_activation_fault_keeps_previous_head_and_history(self):
        old = self.ready()
        self.call(ACTIVATE, {'candidate_ref': old, 'expected_generation': 0, 'reason': '第一版'})
        new = self.ready()
        with patch('governance.services.write_audit', side_effect=RuntimeError('injected audit failure')):
            with self.assertRaises(RuntimeError):
                self.call(ACTIVATE, {'candidate_ref': new, 'expected_generation': 1, 'reason': '第二版'})
        self.assertEqual(uuid.UUID(old), PolicyHead.objects.get(store=self.a).candidate_id)
        self.assertEqual(1, Activation.objects.count())

    def test_stale_generation_is_rejected_without_overwriting_policy(self):
        first, second = self.ready(), self.ready()
        self.call(ACTIVATE, {'candidate_ref': first, 'expected_generation': 0, 'reason': '启用'})
        with self.assertRaises(DomainError):
            self.call(ACTIVATE, {'candidate_ref': second, 'expected_generation': 0, 'reason': '过期'})
        self.assertEqual(uuid.UUID(first), PolicyHead.objects.get(store=self.a).candidate_id)

    def test_http_generation_and_activation_errors_preserve_state_and_audit_reason(self):
        self.client.force_login(self.manager)

        def post(path, payload):
            return self.client.post(path, json.dumps(payload), content_type='application/json',
                                    HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))

        foreign = copy.deepcopy(self.definition)
        foreign['scope']['store_id'] = str(self.b.pk)
        response = post('/api/candidates/generate', {'definition': foreign, 'reason': '跨店生成'})
        self.assertEqual((403, 'SCOPE_DENIED'), (response.status_code, response.json()['error']['code']))
        self.assertFalse(Candidate.objects.exists())
        self.assertFalse(Demand.objects.exists())
        self.assertFalse(Operation.objects.exists())
        self.assertTrue(AuditEvent.objects.filter(actor=self.manager, store=self.a,
                                                  normalized_reason='SCOPE_DENIED').exists())

        unapproved = self.generate()
        response = post('/api/candidates/activate', {'candidate_ref': unapproved,
                                                      'expected_generation': 0, 'reason': '未批准'})
        self.assertEqual((409, 'APPROVAL_REQUIRED'), (response.status_code, response.json()['error']['code']))
        self.assertFalse(Activation.objects.exists())
        self.assertIsNone(PolicyHead.objects.get(store=self.a).candidate_id)
        self.assertTrue(AuditEvent.objects.filter(actor=self.manager, store=self.a,
                                                  normalized_reason='APPROVAL_REQUIRED').exists())

        first, second = self.ready(), self.ready()
        self.call(ACTIVATE, {'candidate_ref': first, 'expected_generation': 0, 'reason': '启用首版'})
        response = post('/api/candidates/activate', {'candidate_ref': second,
                                                      'expected_generation': 0, 'reason': '过期版本'})
        self.assertEqual((409, 'POLICY_VERSION_CONFLICT'), (response.status_code, response.json()['error']['code']))
        self.assertEqual(uuid.UUID(first), PolicyHead.objects.get(store=self.a).candidate_id)
        self.assertEqual(1, Activation.objects.count())
        self.assertTrue(AuditEvent.objects.filter(actor=self.manager, store=self.a,
                                                  normalized_reason='POLICY_VERSION_CONFLICT').exists())

    def test_valid_rehashed_replacement_cannot_reuse_old_approval(self):
        from vetcontrols import compile_demand
        ref = self.ready()
        changed = copy.deepcopy(self.definition)
        changed['quality_gates']['coverage']['minimum_percent'] = 85
        compiled = compile_demand(changed)
        self.assertTrue(compiled['ok'])
        Candidate.objects.filter(pk=ref).update(envelope=compiled['candidate'], digest=compiled['candidate']['content_digest'])
        with self.assertRaises(DomainError) as raised:
            self.call(ACTIVATE, {'candidate_ref': ref, 'expected_generation': 0, 'reason': '旧批准不可复用'})
        self.assertEqual('APPROVAL_REQUIRED', raised.exception.code)
        self.assertIsNone(PolicyHead.objects.get(store=self.a).candidate_id)

    def test_framework_superuser_never_bypasses_business_roles(self):
        self.doctor.is_staff = True
        self.doctor.is_superuser = True
        self.doctor.save(update_fields=['is_staff', 'is_superuser'])
        with self.assertRaises(DomainError):
            self.call(GENERATE, {'definition': self.definition, 'reason': '角色拒绝'}, actor=self.doctor)

    def test_mysql_is_real_innodb_and_row_lock_backend(self):
        self.assertEqual('mysql', connection.vendor)
        self.assertTrue(connection.features.has_select_for_update)
        with connection.cursor() as cursor:
            cursor.execute("SELECT DISTINCT ENGINE FROM information_schema.tables WHERE table_schema=DATABASE()")
            self.assertEqual({'InnoDB'}, {row[0] for row in cursor.fetchall()})

    def test_web_login_csrf_and_direct_api_permissions(self):
        client = Client(enforce_csrf_checks=True)
        response = client.get('/')
        self.assertEqual(200, response.status_code)
        self.assertEqual(403, client.post('/api/login', {'username': 'manager-a', 'password': 'test-only-password'}).status_code)
        token = client.cookies['csrftoken'].value
        response = client.post('/api/login', {'username': 'manager-a', 'password': 'test-only-password'}, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(200, response.status_code)
        self.assertNotIn('password', response.json())
        self.assertEqual(200, client.get('/api/candidates').status_code)
        Account.objects.filter(pk=self.manager.pk).update(role='doctor')
        self.assertEqual(403, client.get('/api/candidates').status_code)

    def test_direct_plaintext_password_storage_is_rejected_and_redacted(self):
        secret = 'synthetic-plaintext-credential'
        with self.assertRaises(ValueError):
            Account.objects.create(username='unsafe', password=secret, store=self.a, role='doctor')
        self.assertFalse(Account.objects.filter(username='unsafe').exists())
        event = AuditEvent.objects.get(action='identity.credential.persist',
                                       normalized_reason='PLAINTEXT_PASSWORD_REJECTED')
        self.assertEqual((self.a.pk, 'refused', None), (event.store_id, event.outcome, event.actor_id))
        self.assertNotIn(secret, str(event.__dict__))

        original_hash = self.manager.password
        self.manager.password = secret
        with self.assertRaises(ValueError):
            self.manager.save(update_fields=['password'])
        self.manager.refresh_from_db()
        self.assertEqual(original_hash, self.manager.password)
        self.assertEqual(2, AuditEvent.objects.filter(normalized_reason='PLAINTEXT_PASSWORD_REJECTED').count())

        with self.assertRaises(ValueError):
            Account.objects.filter(pk=self.manager.pk).update(password=secret)
        with self.assertRaises(ValueError):
            Account.objects.bulk_create([Account(username='unsafe-bulk', password=secret,
                                                 store=self.a, role='doctor')])
        self.manager.password = secret
        with self.assertRaises(ValueError):
            Account.objects.bulk_update([self.manager], ['password'])
        self.manager.refresh_from_db()
        self.assertEqual(original_hash, self.manager.password)
        self.assertFalse(Account.objects.filter(username='unsafe-bulk').exists())
        self.assertEqual(3, AuditEvent.objects.filter(normalized_reason='CREDENTIAL_WRITE_BYPASS_REJECTED').count())

    def test_sql_injection_shaped_audit_filter_is_rejected_and_redacted(self):
        attack = "1' OR '1'='1"
        self.client.force_login(self.manager)
        response = self.client.get('/api/audit', {'operator_ref': attack})
        self.assertEqual((422, 'INVALID_REFERENCE'), (response.status_code, response.json()['error']['code']))
        self.assertNotIn(attack, response.content.decode())
        event = AuditEvent.objects.get(actor=self.manager, action='audit.metadata.read',
                                       normalized_reason='INVALID_REFERENCE')
        self.assertEqual(self.a.pk, event.store_id)
        self.assertNotIn(attack, str(event.__dict__))

    def test_missing_permission_entry_is_denied_before_view_runs(self):
        module = types.ModuleType('unregistered_test_urls')
        module.urlpatterns = [path('forgotten', lambda request: JsonResponse({'clinical': 'DO-NOT-EXPOSE'}))]
        with override_settings(ROOT_URLCONF=module):
            response = Client().get('/forgotten')
            from .entry import check_entries
            self.assertEqual('governance.E001', check_entries(None)[0].id)
        self.assertEqual(403, response.status_code)
        self.assertNotIn('DO-NOT-EXPOSE', response.content.decode())
        self.assertTrue(AuditEvent.objects.filter(normalized_reason='MISSING_PERMISSION_ENTRY').exists())

    def test_metadata_audit_never_returns_free_text_and_stays_in_store(self):
        self.generate()
        AuditEvent.objects.create(store=self.b, actor=self.other, action='other', outcome='success', normalized_reason='COMPLETED')
        client = Client()
        client.force_login(self.manager)
        data = client.get('/api/audit').json()
        fields = {'event_ref', 'event_at', 'event_store_ref', 'operator_ref', 'operator_role', 'action', 'outcome', 'normalized_reason', 'target_ref'}
        self.assertTrue(data['items'])
        for row in data['items']:
            self.assertEqual(fields, set(row))
            self.assertEqual(str(self.a.pk), row['event_store_ref'])
        self.assertNotIn('定义演示规则', json.dumps(data, ensure_ascii=False))

    def test_wrong_role_refusal_is_visible_only_to_requesting_store_manager(self):
        client = Client()
        client.force_login(self.front)
        response = client.get('/api/clinical/encounters')
        self.assertEqual(403, response.status_code)
        self.assertEqual('PERMISSION_DENIED', response.json()['error']['code'])

        event = AuditEvent.objects.get(actor=self.front, action='clinical.read', normalized_reason='PERMISSION_DENIED')
        self.assertEqual(self.a.pk, event.store_id)
        self.assertIsNone(event.target)

        client.force_login(self.manager)
        local = client.get('/api/audit', {'operator_ref': str(self.front.pk)}).json()['items']
        self.assertEqual([str(event.pk)], [row['event_ref'] for row in local])
        self.assertEqual(str(self.a.pk), local[0]['event_store_ref'])
        client.force_login(self.other)
        foreign = client.get('/api/audit', {'operator_ref': str(self.front.pk)}).json()['items']
        self.assertEqual([], foreign)

    def test_http_lifecycle_and_strict_body_route_and_csrf_boundaries(self):
        client = Client()
        client.force_login(self.manager)
        def post(route, body):
            return client.post('/api/candidates/' + route, data=json.dumps(body), content_type='application/json', HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        created = post('generate', {'definition': self.definition, 'reason': '生成'})
        self.assertEqual(200, created.status_code)
        ref = created.json()['candidate_ref']
        self.assertEqual(200, post('submit', {'candidate_ref': ref, 'reason': '提交'}).status_code)
        data = client.get('/api/candidates/' + ref).json()
        self.assertEqual(200, post('review', {'candidate_ref': ref, 'content_digest': data['content_version_ref'], 'decision': 'approve', 'reason': '核对后批准'}).status_code)
        self.assertEqual(200, post('activate', {'candidate_ref': ref, 'expected_generation': 0, 'reason': '手动启用'}).status_code)
        self.assertEqual(422, post('submit', {'definition': self.definition, 'reason': '错误路由'}).status_code)
        self.assertEqual(422, post('generate', {'definition': '{"scope":{},"scope":{}}', 'reason': '重复键'}).status_code)
        duplicate = client.post('/api/candidates/generate', data='{"reason":"a","reason":"b"}', content_type='application/json', HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        self.assertEqual(422, duplicate.status_code)
        for value in [[], None, {'actor_id': self.other.pk}]:
            self.assertEqual(422, post('generate', value).status_code)

    def test_two_managers_racing_one_generation_have_one_winner(self):
        first, second = self.ready(), self.ready()
        manager2 = Account.objects.create_user(username='manager-a2', password='test-only-password', store=self.a, role='manager')
        barrier = Barrier(2)
        def worker(actor, ref):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                result = self.call(ACTIVATE, {'candidate_ref': ref, 'expected_generation': 0, 'reason': '并发启用'}, actor=actor)
                return result['generation']
            except DomainError as error:
                return error.code
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, actor, ref) for actor, ref in [(self.manager, first), (manager2, second)]]
            outcomes = [future.result(timeout=10) for future in futures]
        self.assertCountEqual([1, 'POLICY_VERSION_CONFLICT'], outcomes)
        self.assertEqual(1, Activation.objects.count())
        self.assertEqual(1, PolicyHead.objects.get(store=self.a).generation)

    def test_real_mysql_head_lock_blocks_then_retry_succeeds(self):
        ref = self.ready()
        key = str(uuid.uuid4())
        def worker():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SET SESSION innodb_lock_wait_timeout=1')
                self.call(ACTIVATE, {'candidate_ref': ref, 'expected_generation': 0, 'reason': '锁等待'}, key=key)
                return 'unexpected_success'
            except OperationalError as error:
                return error.args[0]
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                PolicyHead.objects.select_for_update().get(store=self.a)
                self.assertEqual(1205, pool.submit(worker).result(timeout=8))
        self.assertEqual(0, Activation.objects.count())
        result = self.call(ACTIVATE, {'candidate_ref': ref, 'expected_generation': 0, 'reason': '锁等待'}, key=key)
        self.assertEqual(1, result['generation'])

    def test_account_authorization_allows_parallel_checks_and_serializes_deactivation(self):
        if connection.vendor != 'mysql':
            self.skipTest('Requires MySQL row-lock behavior')
        actor = Account.objects.create_user(username='shared-lock-doctor', password='test-only-password',
                                            store=self.a, role='doctor')
        acquired = Event()
        release = Event()
        second_acquired = Event()
        revocation_started = Event()
        revoked = Event()

        def hold_authorization():
            close_old_connections()
            try:
                with transaction.atomic():
                    require(actor.pk, 'clinical.read', lock=True)
                    acquired.set()
                    if not release.wait(timeout=8):
                        raise TimeoutError('authorization lock release timed out')
            finally:
                connection.close()

        def second_authorization():
            close_old_connections()
            try:
                with transaction.atomic():
                    require(actor.pk, 'clinical.read', lock=True)
                    second_acquired.set()
            finally:
                connection.close()

        def deactivate():
            close_old_connections()
            try:
                revocation_started.set()
                Account.objects.filter(pk=actor.pk).update(is_active=False)
                revoked.set()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(hold_authorization)
            self.assertTrue(acquired.wait(timeout=5))
            second = pool.submit(second_authorization)
            try:
                self.assertTrue(second_acquired.wait(timeout=1),
                                'same-account authorization should use compatible shared locks')
                deactivation = pool.submit(deactivate)
                self.assertTrue(revocation_started.wait(timeout=3))
                self.assertFalse(revoked.wait(timeout=0.2),
                                 'account deactivation must wait for in-flight authorization')
            finally:
                release.set()
            first.result(timeout=5)
            second.result(timeout=5)
            if 'deactivation' in locals():
                deactivation.result(timeout=5)

        self.assertTrue(revoked.is_set())
        with self.assertRaises(DomainError) as caught:
            require(actor.pk, 'clinical.read')
        self.assertEqual('AUTH_REQUIRED', caught.exception.code)
