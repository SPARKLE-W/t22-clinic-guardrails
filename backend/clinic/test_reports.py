import json
import uuid
from datetime import timedelta
from unittest.mock import patch
from django.db import DatabaseError
from django.utils import timezone
from governance.models import Account, AuditEvent
from .models import Alert, AlertOutbox, ClinicalCheck, Encounter, GuardHit, PetIdentity
from .tests import ClinicFixture
from .writes import diagnosis, resolve


class StoreSummaryTests(ClinicFixture):
    url = '/api/reports/store-summary'

    def setUp(self):
        super().setUp()
        self.ref = self.create()['encounter_ref']
        self.marked = self.create()['encounter_ref']
        self.key = str(uuid.uuid4())
        diagnosis(self.doctor.pk, 'record', self.key, {}, self.ref)
        diagnosis(self.doctor.pk, 'record', self.key, {}, self.ref)
        diagnosis(self.doctor.pk, 'record', str(uuid.uuid4()), {}, self.ref)
        Encounter.objects.filter(pk=self.marked).update(error_mark=True, error_reason='PRIVATE_ERROR_REASON')
        alert = Alert.objects.order_by('id').first()
        resolve(self.doctor.pk, str(uuid.uuid4()), {'alert_ref': str(alert.pk), 'handling_result': 'PRIVATE_HANDLING'}, str(alert.pk))
        self.client.force_login(self.manager)

    def test_own_store_counts_groups_and_redaction(self):
        pet = PetIdentity.objects.create(store=self.b)
        foreign = Encounter.objects.create(store=self.b, pet=pet, receptionist=self.front,
                                           doctor=self.foreign, template=self.template,
                                           species='PRIVATE_SPECIES', complaint='PRIVATE_COMPLAINT')
        check = ClinicalCheck.objects.create(encounter=foreign, actor=self.foreign, mode='record',
                                              template=self.template, policy=self.template,
                                              policy_digest=self.template.digest, status='blocked', checks=[])
        hit = GuardHit.objects.create(evaluation=check, rule_ref='FOREIGN_RULE', rule_version='1',
                                      field='diagnosis', value='PRIVATE_CLINICAL_VALUE')
        Alert.objects.create(hit=hit)
        response = self.client.get(self.url)
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual({'time_range', 'encounter_created_count', 'error_marked_count', 'hit_count',
                          'alert_count', 'unhandled_count', 'grouped_counts'}, set(data))
        self.assertEqual([2, 1, 2, 2, 1], [data[k] for k in ['encounter_created_count', 'error_marked_count',
                                                           'hit_count', 'alert_count', 'unhandled_count']])
        self.assertEqual({'group_by': 'rule', 'items': [
            {'rule_ref': 'SR-02', 'hit_count': 2, 'alert_count': 2, 'unhandled_count': 1}]}, data['grouped_counts'])
        self.assertNotIn('PRIVATE_', response.content.decode())
        self.assertNotIn(str(pet.pk), response.content.decode())
        event = AuditEvent.objects.get(action='report.store_summary.read', outcome='success')
        self.assertEqual(self.a.pk, event.store_id)
        self.assertIsNone(event.target)

    def test_handling_state_group_does_not_group_encounters(self):
        response = self.client.get(self.url, {'group_by': 'handling_state'})
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(2, data['encounter_created_count'])
        self.assertEqual({'group_by': 'handling_state', 'items': [
            {'handling_state': 'unhandled', 'alert_count': 1},
            {'handling_state': 'handled', 'alert_count': 1}]}, data['grouped_counts'])

    def test_time_range_is_half_open_and_uses_check_time_for_hits(self):
        start = timezone.now().replace(microsecond=0)
        end = start + timedelta(hours=1)
        Encounter.objects.filter(pk=self.ref).update(created_at=start)
        Encounter.objects.filter(pk=self.marked).update(created_at=end)
        checks = list(ClinicalCheck.objects.order_by('id'))
        ClinicalCheck.objects.filter(pk=checks[0].pk).update(created_at=start)
        ClinicalCheck.objects.filter(pk=checks[1].pk).update(created_at=end)
        interval = {'from': start.isoformat(), 'to': end.isoformat()}
        response = self.client.get(self.url, {'time_range': json.dumps(interval)})
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(interval, data['time_range'])
        self.assertEqual([1, 0, 1, 1], [data[k] for k in ['encounter_created_count', 'error_marked_count', 'hit_count', 'alert_count']])

    def test_empty_store_reports_zero_instead_of_failure_or_global_data(self):
        other = Account.objects.create_user(username='manager-b-report', store=self.b, role='manager')
        self.client.force_login(other)
        response = self.client.get(self.url)
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual([0]*5, [data[k] for k in ['encounter_created_count', 'error_marked_count', 'hit_count', 'alert_count', 'unhandled_count']])
        self.assertEqual([], data['grouped_counts']['items'])

    def test_anonymous_and_other_roles_are_denied(self):
        self.client.logout()
        self.assertEqual(401, self.client.get(self.url).status_code)
        for actor in [self.front, self.doctor]:
            self.client.force_login(actor)
            self.assertEqual(403, self.client.get(self.url).status_code)

    def test_unknown_identity_detail_and_grouping_filters_are_rejected(self):
        for query in [{'store_ref': str(self.b.pk)}, {'record_ref': str(uuid.uuid4())},
                      {'pet_ref': str(uuid.uuid4())}, {'rule_ref': 'SR-02'}, {'group_by': 'doctor'}]:
            with self.subTest(query=query):
                response = self.client.get(self.url, query)
                self.assertEqual(422, response.status_code)
                self.assertNotIn('encounter_created_count', response.json())
        events = AuditEvent.objects.filter(action='report.store_summary.read', outcome='request_refused')
        self.assertEqual(5, events.count())
        self.assertEqual({self.a.pk}, set(events.values_list('store_id', flat=True)))

    def test_invalid_time_ranges_and_repeated_parameters_are_rejected(self):
        for interval in ['not-json', '{}', 'null', '[]',
                         json.dumps({'from': '2026-01-01', 'to': '2026-01-02'}),
                         json.dumps({'from': '2026-01-02T00:00:00Z', 'to': '2026-01-01T00:00:00Z'})]:
            with self.subTest(interval=interval):
                self.assertEqual(422, self.client.get(self.url, {'time_range': interval}).status_code)
        self.assertEqual(422, self.client.get(self.url+'?group_by=rule&group_by=handling_state').status_code)

    def test_delivery_attempts_and_receipt_retries_do_not_increase_counts(self):
        from .models import DeliveryAttempt, DeliveryReceipt
        before = self.client.get(self.url)
        self.assertEqual(200, before.status_code)
        event = AlertOutbox.objects.first()
        for _ in range(3):
            DeliveryAttempt.objects.create(pk=uuid.uuid4(), event=event, outcome='uncertain')
        DeliveryReceipt.objects.create(event=event, received_count=4)
        AlertOutbox.objects.filter(pk=event.pk).update(state='delivered', attempt_count=3)
        self.assertEqual(before.json(), self.client.get(self.url).json())

    def test_database_and_audit_failures_never_return_zero_or_counts(self):
        for target in ['clinic.reports.GuardHit.objects.filter', 'clinic.reports.write_audit']:
            with self.subTest(target=target), patch(target, side_effect=DatabaseError('synthetic failure')):
                response = self.client.get(self.url)
                self.assertEqual(503, response.status_code)
                self.assertNotIn('encounter_created_count', response.json())

    def test_mutating_methods_are_not_available(self):
        self.assertEqual(405, self.client.post(self.url, {}, content_type='application/json').status_code)
