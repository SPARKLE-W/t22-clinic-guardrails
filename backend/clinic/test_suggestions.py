import uuid
from io import StringIO
from unittest.mock import patch
from django.core.management import call_command
from django.db import DatabaseError
from django.test import Client
from governance.models import AuditEvent, Operation, PolicyHead
from .tests import ClinicFixture
from .models import Alert, AlertOutbox, ClinicalRecord, ClinicalRevision, Encounter, MedicationEntry, SuggestionDecision


class SuggestionTests(ClinicFixture):
    def setUp(self):
        super().setUp()
        self.ref = self.create()['encounter_ref']

    def listing(self, actor=None, ref=None):
        client = Client(); client.force_login(actor or self.doctor)
        return client.get('/api/clinical/encounters/'+(ref or self.ref)+'/suggestions')

    def seed(self):
        call_command('seed_demo_suggestions', encounter_ref=self.ref, stdout=StringIO())
        result = self.listing()
        self.assertEqual(200, result.status_code)
        return result.json()['items']

    def adoption_payload(self, item, content=None):
        return {'encounter_ref':self.ref, 'suggestion_ref':item['suggestion_ref'],
                'adopted_content':content or {'diagnosis':'医生核对后的合成诊断'}, 'reason':'合成采纳核对'}

    def post(self, item, payload=None, mode='adoption', actor=None, key=None):
        client = Client(); client.force_login(actor or self.doctor)
        return client.post('/api/clinical/suggestions/'+item['suggestion_ref']+'/'+mode,
                           payload if payload is not None else self.adoption_payload(item), content_type='application/json',
                           HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))

    def test_empty_list_does_not_manufacture_a_formal_record(self):
        response=self.listing()
        self.assertEqual(200,response.status_code)
        self.assertEqual([],response.json()['items'])
        self.assertFalse(ClinicalRecord.objects.exists())

    def test_demo_seed_is_idempotent_and_never_writes_clinical_content(self):
        first=self.seed();second=self.seed()
        self.assertEqual(first,second)
        self.assertEqual(2,len(first))
        self.assertTrue(all(item['source']['type']=='demo' and item['status']=='pending' for item in first))
        self.assertIn('合成演示',first[0]['source']['label'])
        self.assertFalse(ClinicalRecord.objects.exists())

    def test_first_adoption_preserves_original_and_returns_only_references(self):
        item=self.seed()[0]
        result=self.post(item)
        self.assertEqual(200,result.status_code)
        self.assertEqual('adopted',result.json()['operation_state'])
        self.assertNotIn('diagnosis',result.json())
        saved=self.listing().json()['items'][0]
        self.assertEqual(item['content'],saved['content'])
        self.assertEqual(item['source'],saved['source'])
        self.assertEqual(self.doctor.pk,saved['decision']['actor_ref'])
        self.assertEqual('合成采纳核对',saved['decision']['reason'])
        record=ClinicalRecord.objects.get()
        self.assertEqual('医生核对后的合成诊断',ClinicalRevision.objects.get(record=record).diagnosis)
        from .services import read
        clinical=read(self.doctor.pk,clinical=True,ref=self.ref)['clinical']
        self.assertEqual(1,len(clinical['adoptions']))
        self.assertEqual(saved['suggestion_ref'],clinical['adoptions'][0]['suggestion_ref'])

    def test_active_template_is_validated_once_for_suggestion_adoption(self):
        item = self.seed()[0]
        from clinic import suggestions
        with patch('clinic.suggestions._validated', wraps=suggestions._validated) as validate:
            response = self.post(item)
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, validate.call_count)

    def test_adoption_on_extended_template_preserves_optional_fields(self):
        template = self.template_fixture('template-extra-adoption', extra_fields=[
            {'id':'x_followup_note','type':'text','required':False,'visible_role':'doctor'}])
        PolicyHead.objects.filter(store=self.a).update(candidate=template, generation=2)
        self.ref = self.create()['encounter_ref']
        items = self.seed()
        first = self.post(items[0], self.adoption_payload(items[0], {
            'diagnosis':'医生核对的合成诊断', 'extra_fields':{'x_followup_note':'三日后复诊'}}))
        self.assertEqual(200, first.status_code)
        record = ClinicalRecord.objects.get(encounter_id=self.ref)
        current = ClinicalRevision.objects.get(record=record)
        self.assertEqual({'x_followup_note':'三日后复诊'}, current.extra_fields)
        second_payload = {**self.adoption_payload(items[1], {
            'extra_fields':{'x_followup_note':'五日后复诊'}, 'original_version_ref':str(current.pk)}),
            'record_ref':str(record.pk)}
        self.assertEqual(200, self.post(items[1], second_payload).status_code)
        self.assertEqual({'x_followup_note':'五日后复诊'}, ClinicalRevision.objects.get(record=record,version=2).extra_fields)
        self.assertEqual({'x_followup_note':'三日后复诊'}, ClinicalRevision.objects.get(pk=current.pk).extra_fields)

    def test_decline_without_reason_survives_missing_policy_and_error_mark(self):
        item=self.seed()[0]
        Encounter.objects.filter(pk=self.ref).update(error_mark=True)
        PolicyHead.objects.filter(store=self.a).update(candidate=None)
        result=self.post(item,{'encounter_ref':self.ref,'suggestion_ref':item['suggestion_ref'],'decision':'declined'},'decline')
        self.assertEqual(200,result.status_code)
        self.assertEqual('declined',self.listing().json()['items'][0]['status'])
        self.assertFalse(ClinicalRecord.objects.exists())
        self.assertFalse(Alert.objects.exists())

    def test_missing_reason_is_durable_sr04_and_retry_is_not_new_alert(self):
        item=self.seed()[0];values=self.adoption_payload(item);values.pop('reason');key=str(uuid.uuid4())
        result=self.post(item,values,key=key)
        self.assertEqual(422,result.status_code)
        self.assertEqual('GUARD_BLOCKED',result.json()['error']['code'])
        self.assertEqual('SR-04',Alert.objects.get().hit.rule_ref)
        self.assertEqual(1,AlertOutbox.objects.count())
        self.assertEqual(result.json(),self.post(item,values,key=key).json())
        self.assertEqual(1,Alert.objects.count())
        self.assertFalse(ClinicalRecord.objects.exists())
        self.assertEqual('pending',self.listing().json()['items'][0]['status'])
        self.assertEqual(200,self.post(item).status_code)

    def test_first_adoption_and_medication_group_require_real_template_fields(self):
        item=self.seed()[0]
        values=self.adoption_payload(item,{'medication':{'drug':'合成药'}})
        result=self.post(item,values)
        self.assertEqual(422,result.status_code)
        self.assertEqual({'diagnosis','amount','unit'},set(Alert.objects.values_list('hit__field',flat=True)))
        self.assertFalse(ClinicalRecord.objects.exists())
        self.assertEqual('pending',self.listing().json()['items'][0]['status'])

    def test_adoption_keeps_old_diagnosis_history_and_appends_medication(self):
        record=self.record_fixture(self.ref);current=ClinicalRevision.objects.get(record=record)
        item=self.seed()[0]
        content={'diagnosis':'修订合成诊断','original_version_ref':str(current.pk),
                 'medication':{'drug':'合成药品','amount':'1','unit':'演示单位'}}
        values={**self.adoption_payload(item,content),'record_ref':str(record.pk)}
        self.assertEqual(200,self.post(item,values).status_code)
        self.assertEqual(2,ClinicalRevision.objects.filter(record=record).count())
        current.refresh_from_db()
        self.assertEqual('仅医生可见的合成诊断',current.diagnosis)
        self.assertEqual('合成检查',ClinicalRevision.objects.get(record=record,version=2).examination)
        self.assertEqual(self.doctor.pk,MedicationEntry.objects.get().actor_id)

    def test_medication_only_adoption_does_not_increment_diagnosis_version(self):
        record=self.record_fixture(self.ref);current=ClinicalRevision.objects.get(record=record)
        item=self.seed()[0]
        values=self.adoption_payload(item,{'medication':{'drug':'合成药','amount':'1','unit':'演示单位'},'original_version_ref':str(current.pk)})
        values['record_ref']=str(record.pk)
        self.assertEqual(200,self.post(item,values).status_code)
        self.assertEqual(1,ClinicalRevision.objects.filter(record=record).count())
        self.assertEqual(1,MedicationEntry.objects.count())

    def test_stale_clinical_reference_and_new_record_after_read_are_conflicts(self):
        item=self.seed()[0]
        record=self.record_fixture(self.ref);current=ClinicalRevision.objects.get(record=record)
        self.assertEqual(409,self.post(item).status_code)
        values=self.adoption_payload(item,{'diagnosis':'新值','original_version_ref':str(uuid.uuid4())})
        values['record_ref']=str(record.pk)
        self.assertEqual(409,self.post(item,values).status_code)
        values['adopted_content']['original_version_ref']=str(current.pk)
        self.assertEqual(200,self.post(item,values).status_code)

    def test_role_scope_and_cross_store_refusal_keep_suggestion_private(self):
        item=self.seed()[0]
        for actor in [self.front,self.manager,self.doctor2,self.foreign]:
            self.assertEqual(403,self.listing(actor).status_code)
            self.assertEqual(403,self.post(item,actor=actor).status_code)
        event=AuditEvent.objects.filter(actor=self.foreign,action='clinical.suggestion.adopt').latest('created_at')
        self.assertEqual(self.b.pk,event.store_id)
        self.assertEqual(uuid.UUID(self.ref),event.target)
        self.assertFalse(ClinicalRecord.objects.exists())

    def test_parent_body_reference_and_untrusted_source_are_rejected(self):
        item=self.seed()[0];values=self.adoption_payload(item)
        self.assertEqual(403,self.post(item,{**values,'encounter_ref':self.create()['encounter_ref']}).status_code)
        self.assertEqual(422,self.post(item,{**values,'source':{'type':'model'}}).status_code)
        self.assertEqual(422,self.post(item,{**values,'adopted_content':{'diagnosis':'x','actor_ref':self.doctor.pk}}).status_code)
        self.assertEqual(403,self.post(item,{**values,'suggestion_ref':str(uuid.uuid4())}).status_code)

    def test_final_decision_replay_content_conflict_and_current_authorization(self):
        item=self.seed()[0];key=str(uuid.uuid4());values=self.adoption_payload(item)
        first=self.post(item,values,key=key)
        self.assertEqual(200,first.status_code)
        self.assertEqual(first.json(),self.post(item,values,key=key).json())
        changed_replay=self.post(item,{**values,'reason':'其他理由'},key=key)
        self.assertEqual((409,'OPERATION_CONTENT_CONFLICT'),
                         (changed_replay.status_code,changed_replay.json()['error']['code']))
        already_final=self.post(item,values)
        self.assertEqual((409,'DECISION_FINAL'),
                         (already_final.status_code,already_final.json()['error']['code']))
        decline={'encounter_ref':self.ref,'suggestion_ref':item['suggestion_ref'],'decision':'declined'}
        declined_after_adoption=self.post(item,decline,'decline')
        self.assertEqual((409,'DECISION_FINAL'),
                         (declined_after_adoption.status_code,declined_after_adoption.json()['error']['code']))
        self.assertEqual(1,SuggestionDecision.objects.count())
        self.assertEqual(1,Operation.objects.filter(permission='clinical.suggestion.adopt').count())
        self.assertFalse(Operation.objects.filter(permission='clinical.suggestion.decline').exists())
        for action,code in [('clinical.suggestion.adopt','OPERATION_CONTENT_CONFLICT'),
                            ('clinical.suggestion.adopt','DECISION_FINAL'),
                            ('clinical.suggestion.decline','DECISION_FINAL')]:
            self.assertTrue(AuditEvent.objects.filter(actor=self.doctor,store=self.a,
                action=action,normalized_reason=code,outcome='refused').exists())
        Encounter.objects.filter(pk=self.ref).update(doctor=self.doctor2)
        self.assertEqual(403,self.post(item,values,key=key).status_code)

    def test_error_mark_disallows_adoption(self):
        item=self.seed()[0]
        Encounter.objects.filter(pk=self.ref).update(error_mark=True)
        self.assertEqual(409,self.post(item).status_code)
        self.assertEqual('pending',self.listing().json()['items'][0]['status'])

    def test_audit_or_outbox_failure_rolls_back_all_adoption_state(self):
        item=self.seed()[0]
        with patch('clinic.suggestions._audit',side_effect=DatabaseError('injected')):
            self.assertEqual(503,self.post(item).status_code)
        with patch.object(AlertOutbox.objects,'create',side_effect=DatabaseError('injected')):
            values=self.adoption_payload(item);values.pop('reason')
            self.assertEqual(503,self.post(item,values).status_code)
        self.assertFalse(ClinicalRecord.objects.exists())
        self.assertFalse(Alert.objects.exists())
        self.assertEqual('pending',self.listing().json()['items'][0]['status'])

    def test_read_audit_failure_does_not_return_suggestion(self):
        self.seed()
        with patch('clinic.suggestions._audit',side_effect=DatabaseError('injected')):
            response=self.listing()
        self.assertEqual(503,response.status_code)
        self.assertNotIn('items',response.json())

    def test_concurrent_same_key_creates_one_decision_and_one_record(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections
        item=self.seed()[0];barrier=Barrier(2);key=str(uuid.uuid4())
        def task(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return self.post(item,key=key).json()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(task,[0,1]))
        self.assertEqual('adopted',results[0]['operation_state'])
        self.assertEqual(results[0],results[1])
        self.assertEqual(1,ClinicalRevision.objects.count())

    def test_direct_edit_or_delete_is_not_a_decision_endpoint(self):
        item=self.seed()[0];client=Client();client.force_login(self.doctor)
        for method in ['put','patch','delete']:
            response=getattr(client,method)('/api/clinical/suggestions/'+item['suggestion_ref']+'/adoption',{},content_type='application/json')
            self.assertEqual(405,response.status_code)

    def test_missing_current_policy_prevents_adoption_but_not_decline(self):
        item=self.seed()[0]
        PolicyHead.objects.filter(store=self.a).update(candidate=None)
        self.assertEqual(503,self.post(item).status_code)
        self.assertEqual('pending',self.listing().json()['items'][0]['status'])
