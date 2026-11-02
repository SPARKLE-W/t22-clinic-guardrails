import csv
import io
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from django.contrib.auth.models import User
from django.db import connections
from django.test import TransactionTestCase
from .fixtures import create_case, LAB_PASSWORD
from .models import Profile, Encounter, Record, Revision, Medication, Suggestion, Decision, Operation, Audit, AuditTarget, Alert, Outbox, PolicyHead
from .services import write_medical, read_medical, export_medical, audit_metadata, mark_error, activate_probe_version


class PreTransactionTests(TransactionTestCase):
    """Real InnoDB/threads. No SQLite or mocked commit/queue."""
    def setUp(self):
        self.a = create_case('A')
        self.b = create_case('B')
        self.other = User.objects.create_user(username='other_'+uuid.uuid4().hex[:8],password=LAB_PASSWORD)
        Profile.objects.create(user=self.other,store=self.a['store'],role='doctor')

    def call(self, mode='diagnosis', key=None, data=None, user=None, **kwargs):
        payload = {'expected_version':1}
        if mode == 'diagnosis': payload.update(diagnosis='PRE new diagnosis', reason='correction')
        if mode in ('append','correct'): payload.update(drug='PRE drug',amount='1',unit='演示单位')
        if data: payload.update(data)
        return write_medical(user or self.a['doctor'],self.a['encounter'].id,mode,key or uuid.uuid4().hex,payload,**kwargs)

    def test_real_mysql_innodb_and_read_committed(self):
        with connections['default'].cursor() as c:
            c.execute('SELECT @@version, @@transaction_isolation')
            version, isolation = c.fetchone()
            c.execute("SELECT ENGINE FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='probe_record'")
            engine = c.fetchone()[0]
        self.assertTrue(version.startswith('8.'),version)
        self.assertEqual(isolation,'READ-COMMITTED')
        self.assertEqual(engine,'InnoDB')

    def test_allowed_read_is_audited_every_time(self):
        for _ in range(2):
            response = read_medical(self.a['doctor'],self.a['encounter'].id)
            self.assertEqual(response['status'],200)
            self.assertEqual(response['record']['diagnosis'],self.a['record'].diagnosis)
        self.assertEqual(Audit.objects.filter(action='clinical.read').count(),2)
        self.assertEqual(AuditTarget.objects.filter(record=self.a['record']).count(),2)

    def test_three_roles_cross_store_and_other_doctor_denied(self):
        secret = self.a['record'].diagnosis
        for actor in [self.a['frontdesk'],self.a['manager'],self.other,self.b['doctor']]:
            with self.subTest(actor=actor.username):
                response = read_medical(actor,self.a['encounter'].id)
                self.assertEqual(response['status'],403)
                self.assertNotIn(secret,str(response))
        self.assertEqual(Audit.objects.filter(result='denied').count(),4)

    def test_cross_store_denial_belongs_only_to_requester_store(self):
        response = read_medical(self.a['doctor'],self.b['encounter'].id)
        self.assertEqual(response['status'],403)
        a_meta = audit_metadata(self.a['manager'])
        b_meta = audit_metadata(self.b['manager'])
        self.assertEqual(len(a_meta['events']),1)
        self.assertEqual(b_meta['events'],[])
        row = a_meta['events'][0]
        self.assertEqual(set(row),{'id','created_at','event_store_id','actor_id','role','action','result','reason_code','target_ref'})
        self.assertEqual(row['event_store_id'],self.a['store'].id)

    def test_audit_failure_blocks_read_without_data(self):
        response = read_medical(self.a['doctor'],self.a['encounter'].id,audit_fault=True)
        self.assertEqual(response,{'status':503,'code':'AUDIT_UNAVAILABLE'})
        self.assertEqual(Audit.objects.count(),0)

    def test_audit_database_failure_rolls_back_each_medical_path(self):
        suggestion = Suggestion.objects.create(encounter=self.a['encounter'])
        original = Medication.objects.create(record=self.a['record'],drug='old',amount='1',unit='u',actor=self.a['doctor'])
        for mode in ['diagnosis','append','correct','adopt']:
            with self.subTest(mode=mode):
                response = self.call(mode,data={'original_id':str(original.id),'suggestion_id':str(suggestion.id),'reason':'because'},audit_fault=True)
                self.assertEqual(response['status'],503)
                record = Record.objects.get(pk=self.a['record'].pk)
                self.assertEqual(record.version,1)
                self.assertEqual(record.diagnosis,self.a['record'].diagnosis)
                self.assertEqual(Medication.objects.count(),1)
                self.assertEqual(Decision.objects.count(),0)
                self.assertEqual(Revision.objects.filter(record=record).count(),1)
                self.assertEqual(Operation.objects.count(),0)
                self.assertEqual(Audit.objects.count(),0)

    def test_medication_append_and_correction_keep_original(self):
        a = self.call('append')
        self.assertEqual(a['status'],200)
        original = Medication.objects.get(id=a['medication_id'])
        b = self.call('correct',data={'expected_version':2,'original_id':str(original.id),'drug':'corrected','reason':'fix'})
        self.assertEqual(b['status'],200)
        original.refresh_from_db()
        self.assertEqual(original.drug,'PRE drug')
        correction = Medication.objects.get(id=b['medication_id'])
        self.assertEqual(correction.original_id,original.id)
        self.assertEqual(correction.actor_id,self.a['doctor'].id)
        self.assertEqual(Medication.objects.count(),2)

    def test_medication_cannot_reference_other_record(self):
        foreign = Medication.objects.create(record=self.b['record'],drug='secret',amount='1',unit='u',actor=self.b['doctor'])
        response = self.call('correct',data={'original_id':str(foreign.id),'reason':'fix'})
        self.assertEqual(response['status'],403)
        self.assertEqual(Medication.objects.count(),1)

    def test_guard_rejection_keeps_alert_outbox_and_no_medical_change(self):
        key = uuid.uuid4().hex
        response = self.call(key=key,data={'diagnosis':''})
        self.assertEqual(response['status'],422)
        self.assertEqual(response['code'],'GUARD_REJECTED')
        self.a['record'].refresh_from_db()
        self.assertEqual(self.a['record'].version,1)
        self.assertEqual(Alert.objects.count(),1)
        self.assertEqual(Outbox.objects.filter(state='pending').count(),1)
        self.assertFalse(Alert.objects.get().handled)
        self.assertEqual(self.call(key=key,data={'diagnosis':''}),response)
        self.assertEqual(Alert.objects.count(),1)
        self.assertEqual(self.call(data={'diagnosis':''})['status'],422)
        self.assertEqual(Alert.objects.count(),2)

    def test_multiple_rules_same_field_remain_distinct(self):
        operation = Operation.objects.create(actor=self.a['doctor'],permission='x',operation_key='once',fingerprint='a'*64)
        for rule in ['SR-02','PRE-SECOND-RULE']:
            Alert.objects.create(encounter=self.a['encounter'],attempt=operation,rule_id=rule,rule_version=1,field='diagnosis')
        self.assertEqual(Alert.objects.count(),2)

    def test_adoption_requires_reason_and_real_actor_then_retries(self):
        suggestion = Suggestion.objects.create(encounter=self.a['encounter'])
        invalid = self.call('adopt',data={'suggestion_id':str(suggestion.id),'reason':''})
        self.assertEqual(invalid['status'],422)
        self.assertEqual(Decision.objects.count(),0)
        key = uuid.uuid4().hex
        data = {'suggestion_id':str(suggestion.id),'reason':'reviewed'}
        saved = self.call('adopt',key=key,data=data)
        self.assertEqual(saved['status'],200)
        self.assertEqual(self.call('adopt',key=key,data=data),saved)
        decision = Decision.objects.get()
        self.assertEqual(decision.actor_id,self.a['doctor'].id)
        self.assertEqual(decision.state,'adopted')
        self.assertEqual(self.call('decline',data={'suggestion_id':str(suggestion.id)})['status'],409)
        self.assertEqual(Decision.objects.count(),1)

    def test_decline_retains_suggestion_and_does_not_write_record(self):
        suggestion = Suggestion.objects.create(encounter=self.a['encounter'])
        response = self.call('decline',data={'suggestion_id':str(suggestion.id)})
        self.assertEqual(response['status'],200)
        self.assertEqual(Decision.objects.get().state,'declined')
        self.assertTrue(Suggestion.objects.filter(pk=suggestion.pk).exists())
        self.a['record'].refresh_from_db()
        self.assertEqual(self.a['record'].version,1)

    def test_same_key_different_content_target_or_mode_conflicts(self):
        key = uuid.uuid4().hex
        self.assertEqual(self.call('append',key=key)['status'],200)
        self.assertEqual(self.call('append',key=key,data={'amount':'2'})['status'],409)
        self.assertEqual(self.call('correct',key=key,data={'original_id':str(uuid.uuid4()),'reason':'fix'})['status'],409)
        second = Encounter.objects.create(store=self.a['store'],doctor=self.a['doctor'],receptionist=self.a['frontdesk'],pet_id='second')
        conflict = write_medical(self.a['doctor'],second.id,'append',key,{'expected_version':0,'drug':'PRE drug','amount':'1','unit':'演示单位'})
        self.assertEqual(conflict['status'],409)
        self.assertEqual(Medication.objects.count(),1)

    def test_success_then_connection_loss_same_retry_does_not_duplicate(self):
        key = uuid.uuid4().hex
        saved = self.call('append',key=key)
        self.assertEqual(saved['status'],200)
        connections.close_all()  # committed result is deliberately discarded by the caller.
        self.assertEqual(self.call('append',key=key),saved)
        self.assertEqual(Medication.objects.count(),1)

    def test_retry_rechecks_current_account_before_returning_old_result(self):
        key = uuid.uuid4().hex
        self.assertEqual(self.call(key=key)['status'],200)
        User.objects.filter(id=self.a['doctor'].id).update(is_active=False)
        self.assertEqual(self.call(key=key)['status'],401)

    def test_two_first_requests_produce_one_medical_effect(self):
        gate = threading.Barrier(2)
        key = uuid.uuid4().hex
        def worker():
            connections.close_all()
            try:
                gate.wait(timeout=10)
                return self.call('append',key=key)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _:worker(),range(2)))
        self.assertEqual([r['status'] for r in responses],[200,200])
        self.assertEqual(responses[0],responses[1])
        self.assertEqual(Medication.objects.count(),1)
        self.assertEqual(Operation.objects.count(),1)

    def test_same_store_different_encounters_share_policy_lock(self):
        other = Encounter.objects.create(store=self.a['store'],doctor=self.a['doctor'],receptionist=self.a['frontdesk'],pet_id='parallel')
        Record.objects.create(encounter=other,diagnosis='initial')
        barrier = threading.Barrier(2)
        def worker(eid):
            connections.close_all()
            try:
                return write_medical(self.a['doctor'],eid,'diagnosis',uuid.uuid4().hex,{'expected_version':1,'diagnosis':'parallel','reason':'fix'},before_commit=lambda:barrier.wait(timeout=4))
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker,[self.a['encounter'].id,other.id]))
        self.assertEqual([r['status'] for r in results],[200,200])

    def test_activation_waits_for_clinical_shared_policy_lock(self):
        entered = threading.Event()
        release = threading.Event()
        def clinical():
            connections.close_all()
            try:
                return self.call(before_commit=lambda:(entered.set(),release.wait(timeout=5)))
            finally: connections.close_all()
        def activation():
            connections.close_all()
            try:
                from django.db import OperationalError
                with connections['default'].cursor() as c:
                    c.execute('SET SESSION innodb_lock_wait_timeout=1')
                try:
                    return activate_probe_version(self.a['manager'],2)
                except OperationalError as exc:
                    return {'mysql_errno':exc.args[0]}
            finally: connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            clinical_future = pool.submit(clinical)
            self.assertTrue(entered.wait(timeout=5))
            activation_future = pool.submit(activation)
            try:
                # A real MySQL 1205 proves the exclusive lock actually reached
                # the database and waited for the held clinical shared lock.
                self.assertEqual(activation_future.result(timeout=4),{'mysql_errno':1205})
            finally:
                release.set()
            self.assertEqual(clinical_future.result()['status'],200)
        self.assertEqual(activate_probe_version(self.a['manager'],2)['status'],200)
        self.assertEqual(PolicyHead.objects.get(store=self.a['store']).version,2)

    def test_misregistration_blocks_new_treatment_but_allows_existing_correction(self):
        original = Medication.objects.create(record=self.a['record'],drug='old',amount='1',unit='u',actor=self.a['doctor'])
        self.assertEqual(mark_error(self.a['frontdesk'],self.a['encounter'].id,'wrong') ['status'],200)
        self.assertEqual(self.call('append')['status'],409)
        self.assertEqual(self.call('correct',data={'original_id':str(original.id),'reason':'fix'})['status'],200)
        self.assertEqual(read_medical(self.a['doctor'],self.a['encounter'].id)['status'],200)
        self.assertEqual(self.call('diagnosis',data={'expected_version':2,'diagnosis':'correct old','reason':'fix','correction':True})['status'],200)

    def test_export_complete_csv_and_audit_targets(self):
        self.assertEqual(self.call('append')['status'],200)
        response = export_medical(self.a['doctor'],[str(self.a['record'].id)],'PRE purpose, 含逗号')
        self.assertEqual(response['status'],200)
        rows = list(csv.DictReader(io.StringIO(response['csv'])))
        self.assertEqual(len(rows),4)  # current, original revision, new revision, medication
        current = next(r for r in rows if r['kind']=='record')
        self.assertIn('旧版',current['payload'])
        event = Audit.objects.get(action='export.clinical')
        self.assertEqual(AuditTarget.objects.filter(audit=event,record=self.a['record']).count(),1)
        metadata = audit_metadata(self.a['manager'])
        self.assertNotIn('PRE purpose',str(metadata))
        self.assertNotIn('旧版',str(metadata))

    def test_export_mixed_scope_fails_entire_request(self):
        response = export_medical(self.a['doctor'],[str(self.a['record'].id),str(self.b['record'].id)],'purpose')
        self.assertEqual(response['status'],403)
        self.assertNotIn('csv',response)

    def test_export_requires_purpose_and_audit_success(self):
        ids = [str(self.a['record'].id)]
        self.assertEqual(export_medical(self.a['doctor'],ids,'')['status'],422)
        response = export_medical(self.a['doctor'],ids,'purpose',audit_fault=True)
        self.assertEqual(response['status'],503)
        self.assertNotIn('csv',response)

    def test_initial_record_template_and_old_history(self):
        new = Encounter.objects.create(store=self.a['store'],doctor=self.a['doctor'],receptionist=self.a['frontdesk'],pet_id='new')
        invalid = write_medical(self.a['doctor'],new.id,'diagnosis','empty',{'expected_version':0,'diagnosis':''})
        self.assertEqual(invalid['status'],422)
        self.assertFalse(Record.objects.filter(encounter=new).exists())
        valid = write_medical(self.a['doctor'],new.id,'diagnosis','valid',{'expected_version':0,'diagnosis':'first'})
        self.assertEqual(valid['status'],200)
        record = Record.objects.get(encounter=new)
        self.assertEqual(record.template_version,'PRE-template-1')
        self.assertEqual(Revision.objects.filter(record=record).count(),1)

    def test_special_text_is_stored_as_data_not_sql(self):
        text = "正常主诉 O'Reilly; DROP TABLE probe_record; --"
        response = self.call(data={'diagnosis':text})
        self.assertEqual(response['status'],200)
        self.a['record'].refresh_from_db()
        self.assertEqual(self.a['record'].diagnosis,text)
        self.assertEqual(Record.objects.count(),2)

    def test_non_audit_database_fault_is_reported_as_unconfirmed(self):
        def database_fault():
            with connections['default'].cursor() as c:
                c.execute('SELECT definitely_missing_PRE_column FROM probe_record')
        response = self.call(before_commit=database_fault)
        self.assertEqual(response['status'],503)
        self.assertEqual(response['code'],'PERSISTENCE_UNCONFIRMED')
        self.a['record'].refresh_from_db()
        self.assertEqual(self.a['record'].version,1)

    def test_export_rechecks_current_role_and_store_before_exposure(self):
        for field, value in [('role','manager'),('store_id',self.b['store'].id)]:
            with self.subTest(field=field):
                Profile.objects.filter(user=self.a['doctor']).update(role='doctor',store=self.a['store'])
                change=lambda:Profile.objects.filter(user=self.a['doctor']).update(**{field:value})
                response=export_medical(self.a['doctor'],[str(self.a['record'].id)],'lab',before_expose=change)
                self.assertEqual(response['status'],403)
                self.assertNotIn('csv',response)

    def test_misregistration_and_write_compete_on_same_encounter_lock(self):
        entered=threading.Event()
        release=threading.Event()
        def frontdesk():
            connections.close_all()
            try:
                return mark_error(self.a['frontdesk'],self.a['encounter'].id,'PRE error',before_commit=lambda:(entered.set(),release.wait(timeout=5)))
            finally:
                connections.close_all()
        errno=[]
        def observe(execute,sql,params,many,context):
            from django.db import OperationalError
            try: return execute(sql,params,many,context)
            except OperationalError as exc:
                errno.append(exc.args[0])
                raise
        with ThreadPoolExecutor(max_workers=1) as pool:
            marking=pool.submit(frontdesk)
            self.assertTrue(entered.wait(timeout=5))
            try:
                with connections['default'].cursor() as c: c.execute('SET SESSION innodb_lock_wait_timeout=1')
                with connections['default'].execute_wrapper(observe):
                    waiting=self.call('append')
                self.assertEqual(errno,[1205])
                self.assertEqual(waiting['code'],'PERSISTENCE_UNCONFIRMED')
            finally:
                release.set()
            self.assertEqual(marking.result()['status'],200)
        self.assertEqual(self.call('append')['code'],'MISREGISTERED')
        self.assertEqual(Medication.objects.count(),0)
        connections.close_all()
