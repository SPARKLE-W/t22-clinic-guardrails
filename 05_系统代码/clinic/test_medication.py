import uuid
from unittest.mock import patch
from django.db import DatabaseError
from django.test import Client
from .tests import ClinicFixture
from .models import ClinicalRecord, ClinicalRevision, Encounter, Alert, AlertOutbox
from .services import read


class MedicationTests(ClinicFixture):
    def setUp(self):
        super().setUp()
        self.ref = self.create()['encounter_ref']
        self.record_fixture(self.ref)

    def post(self, values, mode='append', actor=None, key=None, ref=None):
        client = Client()
        client.force_login(actor or self.doctor)
        suffix = '/corrections' if mode == 'correct' else ''
        return client.post('/api/clinical/encounters/'+(ref or self.ref)+'/medications'+suffix, values,
                           content_type='application/json', HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))

    def values(self):
        return {'drug': '合成演示药品', 'amount': '1', 'unit': '演示单位'}

    def chain(self):
        return read(self.doctor.pk, clinical=True, ref=self.ref)['clinical']['medication_chain']

    def test_append_response_is_reference_only_and_read_has_real_responsibility(self):
        response = self.post(self.values())
        self.assertEqual(200, response.status_code)
        self.assertNotIn('drug', response.json())
        entry = self.chain()[0]
        self.assertEqual(self.doctor.pk, entry['actor_ref'])
        self.assertEqual('合成演示药品', entry['drug'])
        self.assertIsNone(entry['original_entry_ref'])

    def test_medication_preserves_extended_template_clinical_values(self):
        from governance.models import PolicyHead
        from .writes import diagnosis
        template = self.template_fixture('template-extra-medication', extra_fields=[
            {'id':'x_followup_note','type':'text','required':False,'visible_role':'doctor'}])
        PolicyHead.objects.filter(store=self.a).update(candidate=template, generation=2)
        ref = self.create()['encounter_ref']
        diagnosis(self.doctor.pk, 'record', str(uuid.uuid4()),
                  {'diagnosis':'合成诊断','extra_fields':{'x_followup_note':'一周后复诊'}}, ref)
        self.assertEqual(200, self.post(self.values(), ref=ref).status_code)
        clinical = read(self.doctor.pk, clinical=True, ref=ref)['clinical']
        self.assertEqual({'x_followup_note':'一周后复诊'}, clinical['extra_fields'])
        self.assertEqual(1, len(clinical['medication_chain']))

    def test_corrections_append_and_keep_full_original_chain(self):
        first = self.post(self.values())
        self.assertEqual(200, first.status_code)
        ref = first.json()['saved_ref']
        second = self.post({**self.values(), 'amount':'2', 'original_entry_ref':ref, 'reason':'合成更正'}, 'correct')
        self.assertEqual(200, second.status_code)
        self.assertEqual(ref, second.json()['original_entry_ref'])
        third = self.post({**self.values(), 'amount':'3', 'original_entry_ref':second.json()['saved_ref'], 'reason':'再次核对'}, 'correct')
        self.assertEqual(200, third.status_code)
        rows = self.chain()
        self.assertEqual(['1','2','3'], [r['amount'] for r in rows])
        self.assertEqual(ref, rows[1]['original_entry_ref'])

    def test_role_scope_parent_and_input_projection_are_enforced(self):
        self.assertEqual(200, self.post(self.values()).status_code)
        for actor in [self.front,self.manager,self.doctor2,self.foreign]:
            self.assertEqual(403, self.post(self.values(), actor=actor).status_code)
        self.assertEqual(422,self.post({**self.values(),'actor_ref':self.doctor.pk}).status_code)
        other = self.create()['encounter_ref']
        self.record_fixture(other)
        self.assertEqual(403,self.post({**self.values(),'original_entry_ref':self.chain()[0]['entry_ref'],'reason':'非法父关联'},'correct',ref=other).status_code)
        self.assertEqual(1,len(self.chain()))

    def test_required_medication_group_is_a_durable_guard_hit(self):
        key = str(uuid.uuid4())
        first = self.post({'drug':'合成药'},key=key)
        self.assertEqual(422,first.status_code)
        self.assertEqual('GUARD_BLOCKED',first.json()['error']['code'])
        self.assertEqual(2,Alert.objects.count())
        self.assertEqual(2,AlertOutbox.objects.filter(state='pending').count())
        self.assertEqual(first.json(),self.post({'drug':'合成药'},key=key).json())
        self.assertEqual(2,Alert.objects.count())
        self.assertEqual([],self.chain())

    def test_missing_correction_reason_is_sr03_not_an_overwrite(self):
        saved = self.post(self.values())
        self.assertEqual(200,saved.status_code)
        result = self.post({**self.values(),'original_entry_ref':saved.json()['saved_ref']},'correct')
        self.assertEqual(422,result.status_code)
        self.assertEqual('SR-03',Alert.objects.get().hit.rule_ref)
        self.assertEqual(1,len(self.chain()))

    def test_replay_content_conflict_and_current_authorization(self):
        key = str(uuid.uuid4())
        first = self.post(self.values(),key=key)
        self.assertEqual(200,first.status_code)
        self.assertEqual(first.json(),self.post(self.values(),key=key).json())
        self.assertEqual(409,self.post({**self.values(),'amount':'2'},key=key).status_code)
        Encounter.objects.filter(pk=self.ref).update(doctor=self.doctor2)
        self.assertEqual(403,self.post(self.values(),key=key).status_code)

    def test_error_mark_allows_only_existing_item_correction(self):
        first = self.post(self.values())
        self.assertEqual(200,first.status_code)
        Encounter.objects.filter(pk=self.ref).update(error_mark=True)
        self.assertEqual(409,self.post(self.values()).status_code)
        self.assertEqual(200,self.post({**self.values(),'original_entry_ref':first.json()['saved_ref'],'reason':'误建已有项纠错'},'correct').status_code)

    def test_audit_failure_rolls_back_medication_and_guard_evidence(self):
        with patch('clinic.medications._audit',side_effect=DatabaseError('injected')):
            self.assertEqual(503,self.post(self.values()).status_code)
            self.assertEqual(503,self.post({}).status_code)
        self.assertEqual([],self.chain())
        self.assertFalse(Alert.objects.exists())

    def test_no_record_or_policy_cannot_be_bypassed(self):
        ref = self.create()['encounter_ref']
        self.assertEqual(409,self.post(self.values(),ref=ref).status_code)
        from governance.models import PolicyHead
        PolicyHead.objects.filter(store=self.a).update(candidate=None)
        self.assertEqual(503,self.post(self.values()).status_code)

    def test_concurrent_same_key_is_one_immutable_entry(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections
        from .medications import mutate
        barrier,key=Barrier(2),str(uuid.uuid4())
        def task(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return mutate(self.doctor.pk,'append',key,self.values(),self.ref)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(task,[0,1]))
        self.assertEqual(result[0],result[1])
        self.assertEqual(1,len(self.chain()))

    def test_direct_update_delete_and_outbox_failure_cannot_change_entries(self):
        first=self.post(self.values())
        self.assertEqual(200,first.status_code)
        client=Client();client.force_login(self.doctor)
        for method in ['put','patch','delete']:
            response=getattr(client,method)('/api/clinical/encounters/'+self.ref+'/medications',self.values(),content_type='application/json')
            self.assertEqual(405,response.status_code)
        with patch.object(AlertOutbox.objects,'create',side_effect=DatabaseError('injected')):
            self.assertEqual(503,self.post({}).status_code)
        self.assertEqual(1,len(self.chain()))
        self.assertFalse(Alert.objects.exists())
