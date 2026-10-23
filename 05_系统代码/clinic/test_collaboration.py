import json
import uuid
from unittest.mock import patch
from django.db import DatabaseError
from django.test import Client
from governance.models import Account, AuditEvent, Review
from .tests import ClinicFixture


class WorkDivisionTests(ClinicFixture):
    url = '/api/management/work-divisions'

    def setUp(self):
        super().setUp()
        self.client.force_login(self.manager)
        self.event = Review.objects.get(candidate=self.template)
        self.registration = {
            'work_item_ref': str(self.template.pk), 'stage_ref': 'rule_review',
            'plan_version_ref': None, 'actual_version_ref': None,
            'plan_dimensions': {'content_generation': None, 'data_entry': 'manager', 'human_decision': 'manager'},
            'source_event_refs': [str(self.event.pk)], 'manual_actual_evidence': [], 'reason': '合成登记'}

    def post(self, payload=None, key=None, suffix=''):
        return self.client.post(self.url+suffix, json.dumps(payload or self.registration),
                                content_type='application/json', HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))

    def test_saved_system_evidence_compares_separate_dimensions_and_redacts(self):
        Review.objects.filter(pk=self.event.pk).update(reason='PRIVATE_REVIEW_REASON')
        response = self.post()
        self.assertEqual(200, response.status_code, response.content)
        data = response.json()
        self.assertEqual('consistent', data['comparison']['state'])
        self.assertEqual(self.manager.pk, data['actual_execution_metadata'][0]['actor_ref'])
        self.assertNotIn('PRIVATE_', response.content.decode())
        self.assertEqual([], data['review_history'])

    def test_missing_generation_is_not_comparable_despite_manager_entry(self):
        self.registration['plan_dimensions']['content_generation'] = 'ai'
        data = self.post().json()
        self.assertEqual('not_comparable', data['comparison']['state'])
        self.assertIn('content_generation', data['comparison']['missing_dimensions'])

    def test_difference_is_preserved_after_real_version_bound_review(self):
        self.registration['plan_dimensions']['data_entry'] = 'doctor'
        data = self.post().json()
        self.assertEqual('pending_review', data['comparison']['state'])
        review = {k: data[k] for k in ['work_item_ref', 'stage_ref', 'plan_version_ref', 'actual_version_ref']}
        review.update(redacted_evidence=self.registration['source_event_refs'], conclusion='confirmed_difference', reason='已核对合成差异')
        response = self.post(review, suffix='/reviews')
        self.assertEqual(200, response.status_code, response.content)
        self.assertEqual('pending_review', response.json()['comparison']['state'])
        self.assertEqual('confirmed_difference', response.json()['review_history'][0]['conclusion'])
        self.registration.update({k: data[k] for k in ['plan_version_ref', 'actual_version_ref']})
        self.registration['plan_dimensions']['data_entry'] = 'manager'
        updated = self.post().json()
        self.assertNotEqual(data['actual_version_ref'], updated['actual_version_ref'])
        self.assertEqual(2, len(updated['maintenance_history']))
        self.assertEqual(data['plan_version_ref'], updated['review_history'][0]['plan_version_ref'])
        self.assertEqual(409, self.post(review, suffix='/reviews').status_code)

    def test_same_key_replays_and_changed_payload_conflicts(self):
        key = str(uuid.uuid4())
        first = self.post(key=key)
        self.assertEqual(200, first.status_code)
        self.assertEqual(first.json(), self.post(key=key).json())
        self.registration['reason'] = '另一个意图'
        self.assertEqual(409, self.post(key=key).status_code)

    def test_manual_claim_cannot_replace_missing_system_source(self):
        self.registration['source_event_refs'] = []
        self.registration['manual_actual_evidence'] = [{'dimension': 'data_entry', 'value': 'manager', 'source_ref': 'manual-note-1'}]
        data = self.post().json()
        self.assertEqual('not_comparable', data['comparison']['state'])
        self.assertEqual('manager_declaration', data['manual_evidence_provenance'][0]['kind'])
        review = {k: data[k] for k in ['work_item_ref', 'stage_ref', 'plan_version_ref', 'actual_version_ref']}
        review.update(redacted_evidence=[], conclusion='confirmed_difference', reason='无证据不能复核成功')
        self.assertEqual(409, self.post(review, suffix='/reviews').status_code)

    def test_unknown_or_other_work_event_cannot_be_attached(self):
        self.registration['source_event_refs'] = [str(uuid.uuid4())]
        self.assertEqual(422, self.post().status_code)
        other = self.template_fixture('other-version')
        self.registration['source_event_refs'] = [str(Review.objects.get(candidate=other).pk)]
        self.assertEqual(422, self.post().status_code)

    def test_foreign_manager_and_clinical_roles_cannot_read_or_modify(self):
        self.assertEqual(200, self.post().status_code)
        for actor in [self.doctor, self.front]:
            self.client.force_login(actor)
            self.assertEqual(403, self.client.get(self.url).status_code)
            self.assertEqual(403, self.post().status_code)
        foreign = Account.objects.create_user(username='foreign-manager', role='manager', store=self.b)
        self.client.force_login(foreign)
        self.assertEqual([], self.client.get(self.url).json()['items'])
        self.assertEqual(403, self.post().status_code)

    def test_evidence_discovery_is_metadata_only_and_scoped(self):
        query = {k: self.registration[k] for k in ['work_item_ref', 'stage_ref']}
        response = self.client.get(self.url+'/evidence', query)
        self.assertEqual(200, response.status_code, response.content)
        self.assertEqual([str(self.event.pk)], response.json()['source_event_refs'])
        self.assertNotIn('reason', response.json()['actual_execution_metadata'][0])
        self.assertEqual(422, self.client.get(self.url, {'store_ref': str(self.b.pk)}).status_code)

    def test_audit_failure_rolls_back_revision_and_operation(self):
        with patch('clinic.collaboration.write_audit', side_effect=DatabaseError('synthetic')):
            self.assertEqual(503, self.post().status_code)
        self.assertEqual([], self.client.get(self.url).json()['items'])
        self.assertEqual(200, self.post().status_code)

    def test_stale_update_does_not_destroy_history(self):
        data = self.post().json()
        self.assertEqual(409, self.post().status_code)
        self.assertEqual(data['plan_version_ref'], self.client.get(self.url).json()['items'][0]['plan_version_ref'])

    def test_demo_generation_is_not_mislabeled_as_real_ai(self):
        from .models import Suggestion
        ref = self.create()['encounter_ref']
        source = Suggestion.objects.create(encounter_id=ref, source_version='test-demo', sample_key='sample',
            source={'type': 'demo', 'version': 'test-demo', 'label': '演示建议'}, content={'diagnosis': 'PRIVATE_CLINICAL'})
        self.registration.update(work_item_ref=ref, stage_ref='content_generation', source_event_refs=[str(source.pk)],
                            plan_dimensions={'content_generation': 'ai', 'data_entry': None, 'human_decision': None})
        response = self.post()
        self.assertEqual(200, response.status_code)
        self.assertEqual('pending_review', response.json()['comparison']['state'])
        self.assertEqual(['demo'], response.json()['comparison']['differences'][0]['actual'])
        self.assertNotIn('PRIVATE_CLINICAL', response.content.decode())
        self.assertEqual(403, self.client.get('/api/clinical/encounters/'+ref).status_code)

    def test_comparator_ai_generation_and_human_entry_are_independent(self):
        from .collaboration import compare
        plan = {'content_generation': 'ai', 'data_entry': 'doctor', 'human_decision': None}
        # Constructed metadata validates the comparison only; no actual model call.
        self.assertEqual('consistent', compare(plan, [{'dimensions': plan}])['state'])
        self.assertEqual('not_comparable', compare(plan, [{'dimensions': dict(plan, content_generation=None)}])['state'])

    def test_clinical_entry_discovery_never_loads_clinical_columns(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        ref = self.create()['encounter_ref']
        self.record_fixture(ref)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url+'/evidence', {'work_item_ref': ref, 'stage_ref': 'clinical_entry'})
        self.assertEqual(200, response.status_code)
        self.assertEqual(self.doctor.pk, response.json()['actual_execution_metadata'][0]['actor_ref'])
        evidence_sql = [q['sql'] for q in queries.captured_queries if 'FROM `clinic_clinicalrevision`' in q['sql']]
        self.assertEqual(1, len(evidence_sql))
        self.assertNotIn('`diagnosis`', evidence_sql[0])
        self.assertNotIn('`examination`', evidence_sql[0])

    def test_review_cannot_fabricate_evidence_or_hide_a_difference(self):
        self.registration['plan_dimensions']['data_entry'] = 'doctor'
        data = self.post().json()
        review = {k: data[k] for k in ['work_item_ref', 'stage_ref', 'plan_version_ref', 'actual_version_ref']}
        review.update(redacted_evidence=[str(uuid.uuid4())], conclusion='confirmed_difference', reason='测试')
        self.assertEqual(409, self.post(review, suffix='/reviews').status_code)
        review.update(redacted_evidence=data['source_event_refs'], conclusion='confirmed_consistent')
        self.assertEqual(409, self.post(review, suffix='/reviews').status_code)

    def test_review_evidence_wrong_shape_is_input_error(self):
        data = self.post().json()
        review = {k: data[k] for k in ['work_item_ref', 'stage_ref', 'plan_version_ref', 'actual_version_ref']}
        review.update(redacted_evidence='bad', conclusion='confirmed_consistent', reason='测试')
        self.assertEqual(422, self.post(review, suffix='/reviews').status_code)

    def test_csrf_missing_key_and_malformed_fields_are_rejected(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.manager)
        self.assertEqual(403, strict.post(self.url, json.dumps(self.registration), content_type='application/json').status_code)
        self.assertEqual(422, self.client.post(self.url, json.dumps(self.registration), content_type='application/json').status_code)
        for patch_data in [{'stage_ref': []}, {'plan_dimensions': {}}, {'source_event_refs': 'not-list'},
                           {'actor_ref': self.doctor.pk}, {'manual_actual_evidence': [{'dimension': 'x'}]}]:
            with self.subTest(patch_data=patch_data):
                self.assertEqual(422, self.post({**self.registration, **patch_data}).status_code)
        self.assertEqual(422, self.client.get(self.url+'?page=1&page=2').status_code)

    def test_manual_reference_rejects_free_text_content(self):
        self.registration['manual_actual_evidence'] = [
            {'dimension': 'data_entry', 'value': 'manager', 'source_ref': '诊断：不应写入来源引用'}]
        self.assertEqual(422, self.post().status_code)

    def test_query_filters_match_only_current_versions(self):
        data = self.post().json()
        self.registration.update(plan_version_ref=data['plan_version_ref'], actual_version_ref=data['actual_version_ref'])
        self.registration['plan_dimensions']['data_entry'] = 'doctor'
        updated = self.post().json()
        self.assertEqual([], self.client.get(self.url, {'comparison_state': 'consistent'}).json()['items'])
        self.assertEqual([], self.client.get(self.url, {'plan_version_ref': data['plan_version_ref']}).json()['items'])
        self.assertEqual(1, self.client.get(self.url, {'actual_version_ref': updated['actual_version_ref']}).json()['total'])

    def test_two_managers_cannot_overwrite_the_same_version(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections, connection
        from governance.services import DomainError
        from .collaboration import mutate
        data = self.post().json()
        self.registration.update(plan_version_ref=data['plan_version_ref'], actual_version_ref=data['actual_version_ref'])
        other = Account.objects.create_user(username='manager2', role='manager', store=self.a)
        barrier = Barrier(2)
        def worker(actor):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                mutate(actor.pk, self.registration, str(uuid.uuid4()))
                return 'saved'
            except DomainError as error:
                return error.code
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, actor) for actor in [self.manager, other]]
            self.assertCountEqual(['saved', 'WORK_VERSION_CONFLICT'], [f.result(timeout=10) for f in futures])
        self.assertEqual(2, len(self.client.get(self.url).json()['items'][0]['maintenance_history']))
