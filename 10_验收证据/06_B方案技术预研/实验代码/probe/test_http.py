import json
import uuid
from django.test import TransactionTestCase
from rest_framework.test import APIClient
from .fixtures import create_case, LAB_PASSWORD
from .models import Audit, Encounter, Record


class HttpContractTests(TransactionTestCase):
    def setUp(self):
        self.case = create_case('http')
        self.client = APIClient(enforce_csrf_checks=True)

    def login(self, role='doctor'):
        first = self.client.get('/probe/login')
        self.assertEqual(first.status_code,200)
        response = self.client.post('/probe/login',{'username':self.case[role].username,'password':LAB_PASSWORD},format='json',HTTP_X_CSRFTOKEN=first.json()['csrf'])
        self.assertEqual(response.status_code,200)
        return self.client.cookies['csrftoken'].value

    def test_real_login_session_and_each_read_audit(self):
        self.login()
        for _ in range(2):
            response = self.client.get(f'/probe/encounters/{self.case["encounter"].id}')
            self.assertEqual(response.status_code,200)
        self.assertEqual(Audit.objects.filter(action='clinical.read').count(),2)

    def test_csrf_blocks_medical_post_without_token(self):
        self.login()
        response = self.client.post(f'/probe/encounters/{self.case["encounter"].id}/diagnosis',{'operation_key':uuid.uuid4().hex,'expected_version':1,'diagnosis':'test','reason':'fix'},format='json')
        self.assertEqual(response.status_code,403)
        self.case['record'].refresh_from_db()
        self.assertEqual(self.case['record'].version,1)

    def test_post_uses_real_actor_and_rejects_unlisted_fields(self):
        token = self.login()
        path = f'/probe/encounters/{self.case["encounter"].id}/append'
        data = {'operation_key':uuid.uuid4().hex,'expected_version':1,'drug':'probe','amount':'1','unit':'u','actor_id':999}
        response = self.client.post(path,data,format='json',HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code,422)
        data.pop('actor_id')
        response = self.client.post(path,data,format='json',HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code,200)
        event = Audit.objects.get(action='medication.record')
        self.assertEqual(event.actor_id,self.case['doctor'].id)

    def test_frontdesk_creates_only_trusted_store_and_receptionist(self):
        token = self.login('frontdesk')
        data = {'operation_key':uuid.uuid4().hex,'pet_id':'PRE-new','species':'dog','complaint':'synthetic','doctor_id':self.case['doctor'].id}
        response = self.client.post('/probe/encounters',data,format='json',HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code,200)
        encounter = Encounter.objects.get(pk=response.json()['encounter_id'])
        self.assertEqual(encounter.store_id,self.case['store'].id)
        self.assertEqual(encounter.receptionist_id,self.case['frontdesk'].id)
        self.assertFalse(Record.objects.filter(encounter=encounter).exists())
        self.assertEqual(self.client.post('/probe/encounters',data,format='json',HTTP_X_CSRFTOKEN=token).json(),response.json())

    def test_export_returns_complete_csv_body(self):
        token = self.login()
        response = self.client.post('/probe/exports',{'record_ids':[str(self.case['record'].id)],'purpose':'lab'},format='json',HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code,200)
        self.assertIn('text/csv',response['Content-Type'])
        self.assertIn('旧版',response.content.decode('utf-8'))
        self.assertEqual(int(response['Content-Length']),len(response.content))

    def test_manager_cannot_read_clinical_and_only_sees_metadata(self):
        self.login('manager')
        response = self.client.get(f'/probe/encounters/{self.case["encounter"].id}')
        self.assertEqual(response.status_code,403)
        metadata = self.client.get('/probe/audit')
        self.assertEqual(metadata.status_code,200)
        self.assertNotIn('diagnosis',metadata.content.decode())
        self.assertNotIn('private_details',metadata.content.decode())

    def test_fault_injection_not_exposed_to_http(self):
        token = self.login()
        response = self.client.post(f'/probe/encounters/{self.case["encounter"].id}/diagnosis',{'operation_key':uuid.uuid4().hex,'expected_version':1,'diagnosis':'new','reason':'fix','audit_fault':True},format='json',HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code,422)

    def test_unauthenticated_request_has_no_clinical_bytes(self):
        response = self.client.get(f'/probe/encounters/{self.case["encounter"].id}')
        self.assertIn(response.status_code,[401,403])
        self.assertNotIn('旧版',response.content.decode())

    def test_invalid_uuid_and_body_types_return_controlled_errors(self):
        self.client.raise_request_exception=False
        login_bad=self.client.get('/probe/login')
        for payload in [[],None,{'username':[],'password':'x'}]:
            response=self.client.post('/probe/login',json.dumps(payload),content_type='application/json',HTTP_X_CSRFTOKEN=login_bad.json()['csrf'])
            self.assertEqual(response.status_code,422)
        token=self.login()
        for payload in [{'record_ids':['not-a-uuid'],'purpose':'lab'},{'record_ids':'wrong-type','purpose':'lab'},{'record_ids':[str(self.case['record'].id)],'purpose':[]}]:
            response=self.client.post('/probe/exports',payload,format='json',HTTP_X_CSRFTOKEN=token)
            self.assertEqual(response.status_code,422)

    def test_json_response_has_complete_content_length(self):
        self.login()
        response=self.client.get(f'/probe/encounters/{self.case["encounter"].id}')
        self.assertTrue(response.has_header('Content-Length'))
        self.assertEqual(int(response['Content-Length']),len(response.content))
