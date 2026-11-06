import uuid
from django.db import connection
from django.test.utils import CaptureQueriesContext
from .models import ClinicalRecord, ClinicalRevision, Encounter, PetIdentity, Suggestion
from .tests import ClinicFixture
from .writes import diagnosis
from .medications import mutate as medication
from .suggestions import mutate as adoption


class WriteStoreQueryTests(ClinicFixture):
    def without_store_read(self, operation):
        with CaptureQueriesContext(connection) as captured:
            result = operation()
        self.assertIsNot(result.get('ok'), False)
        reads = [row['sql'] for row in captured
                 if row['sql'].lstrip().upper().startswith('SELECT') and
                 'governance_store' in row['sql'].lower()]
        self.assertEqual([], reads)
        return result

    def test_new_and_known_pet_reception_use_account_store_id(self):
        first = self.without_store_read(self.create)
        encounter = Encounter.objects.get(pk=first['encounter_ref'])
        self.assertEqual(self.a.pk, encounter.store_id)
        self.assertEqual(self.a.pk, PetIdentity.objects.get(pk=encounter.pet_id).store_id)
        payload = {**self.payload(), 'pet_identity_choice': 'known', 'pet_ref': str(encounter.pet_id)}
        second = self.without_store_read(lambda: self.create(payload))
        self.assertEqual(encounter.pet_id, Encounter.objects.get(pk=second['encounter_ref']).pet_id)

    def test_diagnosis_uses_account_store_id(self):
        ref = self.create()['encounter_ref']
        self.without_store_read(lambda: diagnosis(self.doctor.pk, 'record', str(uuid.uuid4()),
                                                  {'diagnosis': 'synthetic diagnosis'}, ref))
        self.assertEqual(self.a.pk, ClinicalRecord.objects.get(encounter_id=ref).encounter.store_id)

    def test_medication_uses_account_store_id(self):
        ref = self.create()['encounter_ref']
        self.record_fixture(ref)
        result = self.without_store_read(lambda: medication(self.doctor.pk, 'append', str(uuid.uuid4()),
                                         {'drug': 'synthetic', 'amount': '1', 'unit': 'tablet'}, ref))
        self.assertEqual('saved', result['operation_state'])

    def test_adoption_uses_account_store_id(self):
        ref = self.create()['encounter_ref']
        suggestion = Suggestion.objects.create(encounter_id=ref, source={
            'type': 'demo', 'label': 'synthetic source', 'version': 'v1'}, source_version='v1',
            content={'diagnosis': 'synthetic suggestion'})
        payload = {'encounter_ref': ref, 'suggestion_ref': str(suggestion.pk), 'reason': 'synthetic review',
                   'adopted_content': {'diagnosis': 'synthetic adopted diagnosis'}}
        result = self.without_store_read(lambda: adoption(self.doctor.pk, 'adopt', str(uuid.uuid4()),
                                                          payload, str(suggestion.pk)))
        self.assertEqual('adopted', result['operation_state'])

    def test_correction_and_adoption_persist_record_version_and_generation_once(self):
        from .exports import clinical
        for operation in ('correction', 'adoption'):
            with self.subTest(operation=operation):
                ref = self.create()['encounter_ref']
                record = self.record_fixture(ref)
                current = ClinicalRevision.objects.get(record=record, version=1)
                key = str(uuid.uuid4())
                if operation == 'correction':
                    payload = {'original_content_ref': str(record.pk), 'original_version_ref': str(current.pk),
                               'diagnosis': 'synthetic revised diagnosis', 'reason': 'synthetic correction'}
                    command = lambda: diagnosis(self.doctor.pk, 'correct', key, payload, ref)
                else:
                    suggestion = Suggestion.objects.create(encounter_id=ref, source={
                        'type': 'demo', 'label': 'synthetic source', 'version': 'v1'}, source_version='v1',
                        content={'diagnosis': 'synthetic suggestion'})
                    payload = {'encounter_ref': ref, 'record_ref': str(record.pk),
                               'suggestion_ref': str(suggestion.pk), 'reason': 'synthetic review',
                               'adopted_content': {'diagnosis': 'synthetic adopted diagnosis',
                                                   'original_version_ref': str(current.pk)}}
                    command = lambda: adoption(self.doctor.pk, 'adopt', key, payload, str(suggestion.pk))
                with CaptureQueriesContext(connection) as captured:
                    result = command()
                updates = [row['sql'] for row in captured
                           if row['sql'].lstrip().upper().startswith('UPDATE') and
                           'clinic_clinicalrecord' in row['sql'].lower()]
                self.assertEqual(1, len(updates), updates)
                self.assertIn('current_version', updates[0])
                self.assertIn('export_generation', updates[0])
                record.refresh_from_db()
                self.assertEqual((2, 1), (record.current_version, record.export_generation))
                self.assertEqual([1, 2], list(ClinicalRevision.objects.filter(record=record).order_by('version').values_list('version', flat=True)))
                self.assertEqual(result, command())
                record.refresh_from_db()
                self.assertEqual((2, 1), (record.current_version, record.export_generation))
                body, _, count = clinical(self.doctor.pk, {'scope': {'record_refs': [str(record.pk)]},
                                                         'purpose': 'synthetic verification'})
                self.assertEqual(1, count)
                self.assertIn(b'synthetic', body)
