import copy
import json
import uuid
from pathlib import Path
from unittest.mock import patch
from django.test import Client, TransactionTestCase
from django.db import DatabaseError
from governance.models import Account, Activation, AuditEvent, Candidate, Demand, PolicyHead, Review, Store
from governance.services import DomainError
from vetcontrols import compile_demand
from .models import AuditTarget, ClinicalRecord, ClinicalRevision, Encounter, PetIdentity, ReceptionRevision
from .services import auxiliary, mutate, read


class ClinicFixture(TransactionTestCase):
    def setUp(self):
        self.a, self.b = Store.objects.create(name='A'), Store.objects.create(name='B')
        self.manager = Account.objects.create_user(username='m', store=self.a, role='manager')
        self.front = Account.objects.create_user(username='f', store=self.a, role='frontdesk')
        self.front2 = Account.objects.create_user(username='f2', store=self.a, role='frontdesk')
        self.doctor = Account.objects.create_user(username='d', store=self.a, role='doctor')
        self.doctor2 = Account.objects.create_user(username='d2', store=self.a, role='doctor')
        self.foreign = Account.objects.create_user(username='db', store=self.b, role='doctor')
        self.template = self.template_fixture()
        PolicyHead.objects.create(store=self.a, candidate=self.template, generation=1)
        PolicyHead.objects.create(store=self.b)

    def template_fixture(self, version='template-0.2', additional_rules=None):
        definition = json.loads((Path(__file__).parents[1] / 'examples/01_完整定义需求_v0.2.json').read_text(encoding='utf-8'))
        definition['scope']['store_id'] = str(self.a.pk)
        definition['templates']['version'] = version
        if additional_rules is not None:
            definition['additional_rules'] = additional_rules
        result = compile_demand(definition)
        self.assertTrue(result['ok'])
        demand = Demand.objects.create(store=self.a, actor=self.manager, reason='合成测试夹具', input_digest=result['candidate']['input_digest'], definition=definition, report={'ok': True})
        candidate = Candidate.objects.create(store=self.a, demand=demand, envelope=result['candidate'], digest=result['candidate']['content_digest'], state='approved')
        Review.objects.create(candidate=candidate, actor=self.manager, decision='approve', digest=candidate.digest, reason='仅测试夹具')
        Activation.objects.create(candidate=candidate, store=self.a, actor=self.manager, generation=Activation.objects.count()+1, digest=candidate.digest, reason='仅测试夹具')
        return candidate

    def payload(self):
        return {'pet_identity_choice': 'new', 'pet_ref': str(uuid.uuid4()), 'species': '猫', 'complaint': '合成主诉', 'doctor_ref': self.doctor.pk, 'pet_name': '演示宠物'}

    def create(self, payload=None, key=None):
        result = mutate(self.front.pk, 'create', key or str(uuid.uuid4()), payload or self.payload())
        self.assertIn('encounter_ref', result)
        return result

    def record_fixture(self, ref):
        record = ClinicalRecord.objects.create(encounter_id=ref, template=self.template)
        ClinicalRevision.objects.create(record=record, version=1, diagnosis='仅医生可见的合成诊断', examination='合成检查', actor=self.doctor)
        return record

class ClinicTests(ClinicFixture):
    def test_creation_is_snapshot_with_server_owned_fixed_identity(self):
        result = self.create()
        row = Encounter.objects.get(pk=result['encounter_ref'])
        self.assertEqual(self.a.pk, row.store_id)
        self.assertEqual(self.front.pk, row.receptionist_id)
        self.assertEqual(self.doctor.pk, row.doctor_id)
        self.assertEqual(1, ReceptionRevision.objects.filter(encounter=row).count())
        self.assertNotIn('diagnosis', result)

    def test_doctor_reads_only_assigned_context_and_original_record_template(self):
        ref = self.create()['encounter_ref']
        record = self.record_fixture(ref)
        later = self.template_fixture('template-0.3')
        PolicyHead.objects.filter(store=self.a).update(candidate=later, generation=2)
        result = read(self.doctor.pk, clinical=True, ref=ref)
        self.assertEqual('仅医生可见的合成诊断', result['clinical']['diagnosis'])
        self.assertEqual(str(record.pk), result['clinical']['record_ref'])
        self.assertEqual('template-0.2', result['clinical']['template_version_ref'])
        self.assertTrue(AuditTarget.objects.filter(target=record.pk, kind='record').exists())

    def test_scope_and_role_denials_include_c11_without_clinical_values(self):
        ref = self.create()['encounter_ref']
        self.record_fixture(ref)
        for actor in [self.front, self.manager, self.doctor2, self.foreign]:
            with self.assertRaises(DomainError):
                read(actor.pk, clinical=True, ref=ref)
        event = AuditEvent.objects.filter(actor=self.foreign, normalized_reason='TARGET_UNAVAILABLE').latest('created_at')
        self.assertEqual(self.b.pk, event.store_id)
        self.assertEqual(uuid.UUID(ref), event.target)
        self.assertNotIn('仅医生', json.dumps(list(AuditEvent.objects.values()), default=str, ensure_ascii=False))

    def test_frontdesk_lists_only_own_receptions_and_never_nested_clinical(self):
        ref = self.create()['encounter_ref']
        self.record_fixture(ref)
        result = read(self.front.pk)
        self.assertEqual(1, len(result['items']))
        self.assertNotIn('诊断', json.dumps(result, ensure_ascii=False))
        self.assertEqual([], read(self.front2.pk)['items'])
        with self.assertRaises(DomainError):
            read(self.front2.pk, ref=ref)

    def test_minimal_auxiliary_has_no_profile_or_history(self):
        options = auxiliary(self.front.pk, 'doctor_options')
        self.assertEqual({self.doctor.pk, self.doctor2.pk}, {r['doctor_ref'] for r in options['doctor_options']})
        result = self.create()
        value = auxiliary(self.front.pk, 'verify_pet_ref', {'pet_ref': result['pet_ref']})
        self.assertEqual({'pet_ref', 'associable'}, set(value))
        self.assertTrue(value['associable'])
        payload = {**self.payload(), 'pet_identity_choice': 'known', 'pet_ref': result['pet_ref'], 'complaint': '新一次接诊'}
        newer = self.create(payload)
        self.assertEqual(2, Encounter.objects.count())
        self.assertNotEqual(result['encounter_ref'], newer['encounter_ref'])
        foreign_pet = PetIdentity.objects.create(store=self.b)
        self.assertFalse(auxiliary(self.front.pk, 'verify_pet_ref', {'pet_ref': str(foreign_pet.pk)})['associable'])

    def test_creation_rejects_spoofing_wrong_doctor_unknown_fields_and_invalid_template(self):
        for changed in [{'store_ref': str(self.b.pk)}, {'receptionist_ref': self.front2.pk}, {'diagnosis': '不可输入'}, {'doctor_ref': self.foreign.pk}, {'doctor_ref': self.manager.pk}, {'template_ref': str(uuid.uuid4())}, {'complaint': ''}]:
            with self.assertRaises(DomainError):
                self.create({**self.payload(), **changed})
        self.assertEqual(0, Encounter.objects.count())
        self.assertEqual(0, PetIdentity.objects.count())

    def test_correct_keeps_history_and_cannot_change_ownership(self):
        result = self.create()
        ref = result['encounter_ref']
        original = result['reception_versions'][-1]['version_ref']
        updated = mutate(self.front.pk, 'correct', str(uuid.uuid4()), {'complaint': '更正后的合成主诉', 'original_version_ref': original, 'reason': '纠错'}, ref)
        self.assertEqual(2, len(updated['reception_versions']))
        self.assertEqual('合成主诉', updated['reception_versions'][0]['complaint'])
        with self.assertRaises(DomainError):
            mutate(self.front.pk, 'correct', str(uuid.uuid4()), {'doctor_ref': self.doctor2.pk, 'original_version_ref': original, 'reason': '改派'}, ref)
        self.assertEqual(self.doctor.pk, Encounter.objects.get(pk=ref).doctor_id)
        with self.assertRaises(DomainError):
            mutate(self.front.pk, 'correct', str(uuid.uuid4()), {'complaint': '旧版本', 'original_version_ref': original, 'reason': '过期'}, ref)

    def test_error_mark_preserves_record_and_still_allows_reads_and_correction(self):
        result = self.create()
        ref = result['encounter_ref']
        record = self.record_fixture(ref)
        marked = mutate(self.front.pk, 'mark', str(uuid.uuid4()), {'reason': '合成误建'}, ref)
        self.assertTrue(marked['error_mark'])
        self.assertTrue(ClinicalRecord.objects.filter(pk=record.pk).exists())
        self.assertEqual(record.pk.hex, uuid.UUID(read(self.doctor.pk, True, ref)['clinical']['record_ref']).hex)
        fixed = mutate(self.front.pk, 'correct', str(uuid.uuid4()), {'complaint': '纠正误建单资料', 'original_version_ref': marked['reception_versions'][-1]['version_ref'], 'reason': '纠错'}, ref)
        self.assertTrue(fixed['error_mark'])

    def test_retry_and_conflict_recheck_current_permissions(self):
        payload, key = self.payload(), str(uuid.uuid4())
        first = self.create(payload, key)
        self.assertEqual(first, self.create(payload, key))
        self.assertEqual(1, Encounter.objects.count())
        with self.assertRaises(DomainError):
            self.create({**payload, 'complaint': '不同意图'}, key)
        Account.objects.filter(pk=self.front.pk).update(is_active=False)
        with self.assertRaises(DomainError):
            self.create(payload, key)

    def test_write_and_read_audit_faults_do_not_commit_or_release_clinical(self):
        with patch('clinic.services.write_audit', side_effect=DatabaseError('synthetic fault')):
            with self.assertRaises(DatabaseError):
                self.create()
        self.assertEqual(0, Encounter.objects.count())
        ref = self.create()['encounter_ref']
        self.record_fixture(ref)
        client = Client()
        client.force_login(self.doctor)
        with patch('clinic.services.write_audit', side_effect=DatabaseError('synthetic fault')):
            response = client.get('/api/clinical/encounters/' + ref)
        self.assertEqual(503, response.status_code)
        self.assertNotIn('仅医生可见', response.content.decode())

    def test_list_target_links_and_record_operator_audit_query_are_scoped(self):
        ref = self.create()['encounter_ref']
        record = self.record_fixture(ref)
        result = read(self.doctor.pk, clinical=True)
        self.assertEqual(1, len(result['items']))
        client = Client()
        client.force_login(self.manager)
        response = client.get('/api/audit', {'record_ref': str(record.pk), 'operator_ref': str(self.doctor.pk)})
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()['items'])
        self.assertTrue(all(r['operator_ref'] == self.doctor.pk for r in response.json()['items']))

    def test_cross_store_refusal_can_be_found_by_record_without_body_access(self):
        ref = self.create()['encounter_ref']
        record = self.record_fixture(ref)
        client = Client()
        client.force_login(self.foreign)
        self.assertEqual(403, client.get('/api/clinical/encounters/'+ref).status_code)
        manager_b = Account.objects.create_user(username='mb', store=self.b, role='manager')
        client.force_login(manager_b)
        result = client.get('/api/audit', {'record_ref': str(record.pk), 'operator_ref': str(self.foreign.pk)})
        self.assertEqual(200, result.status_code)
        self.assertEqual(1, len(result.json()['items']))
        self.assertNotIn('仅医生可见', result.content.decode())

    def test_http_unknown_fields_invalid_ids_and_no_formal_record_are_explicit(self):
        ref = self.create()['encounter_ref']
        client = Client()
        client.force_login(self.doctor)
        response = client.get('/api/clinical/encounters/' + ref)
        self.assertEqual(200, response.status_code)
        self.assertIsNone(response.json()['clinical'])
        self.assertEqual(422, client.get('/api/clinical/encounters', {'fields': 'diagnosis'}).status_code)
        self.assertEqual(422, client.get('/api/clinical/encounters', {'encounter_ref': 'bad'}).status_code)
        client.force_login(self.front)
        response = client.post('/api/encounters', data='[]', content_type='application/json', HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        self.assertEqual(422, response.status_code)

    def test_stable_pages_cover_scope_and_audit_every_returned_target(self):
        pet = PetIdentity.objects.create(store=self.a)
        rows = [Encounter(store=self.a, pet=pet, receptionist=self.front, doctor=self.doctor, template=self.template, species='猫', complaint='分页夹具') for _ in range(51)]
        Encounter.objects.bulk_create(rows)
        ReceptionRevision.objects.bulk_create([ReceptionRevision(encounter=row, version=1, species='猫', complaint='分页夹具', actor=self.front) for row in rows])
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        with CaptureQueriesContext(connection) as queries:
            first = read(self.doctor.pk, clinical=True, query={'page': 1})
        second = read(self.doctor.pk, clinical=True, query={'page': 2})
        self.assertEqual(51, first['total'])
        self.assertEqual(50, len(first['items']))
        self.assertEqual(1, len(second['items']))
        refs = [r['reception']['encounter_ref'] for r in first['items']+second['items']]
        self.assertEqual(51, len(set(refs)))
        self.assertEqual(51, AuditTarget.objects.filter(kind='encounter', event__action='clinical.read').count())
        self.assertLess(len(queries), 25, 'Page reading must not query once per reception or clinical row')

    def test_empty_references_bad_time_ranges_and_lost_template_fail_explicitly(self):
        self.create()
        for query in [{'encounter_ref': ''}, {'record_ref': ''}, {'time_range': {'from': 'bad', 'to': 'later'}}, {'page': True}, []]:
            with self.assertRaises(DomainError):
                read(self.doctor.pk, clinical=True, query=query)
        Candidate.objects.filter(pk=self.template.pk).update(digest='0'*64)
        with self.assertRaises(DomainError) as caught:
            read(self.doctor.pk, clinical=True)
        self.assertEqual('TEMPLATE_UNAVAILABLE', caught.exception.code)
