import csv
import hashlib
import io
import json
import uuid
from unittest.mock import patch
from django.db import DatabaseError, connection
from governance.models import Account, AuditEvent
from governance.services import DomainError
from .models import (Alert, AuditTarget, ClinicalCheck, ClinicalRecord, ClinicalRevision, Encounter,
                     ExportRequest, MedicationEntry, Suggestion, SuggestionDecision)
from .tests import ClinicFixture
from .exports import _csv, _csv_values, _json_text, clinical, guard, SUMMARY
from .services import read
from .writes import diagnosis


class ClinicalExportTests(ClinicFixture):
    url = '/api/exports/clinical-records'

    def setUp(self):
        super().setUp()
        self.ref = self.create()['encounter_ref']
        self.record = self.record_fixture(self.ref)
        self.client.force_login(self.doctor)

    def download(self, payload):
        return self.client.post(self.url, json.dumps(payload), content_type='application/json')

    def rows(self, response):
        self.assertEqual(200, response.status_code, response.content[:300])
        self.assertTrue(response['Content-Type'].startswith('text/csv'))
        self.assertEqual(len(response.content), int(response['Content-Length']))
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), response['X-Content-SHA256'])
        return list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))

    def test_explicit_authorized_record_is_complete_and_audited(self):
        response = self.download({'scope': {'record_refs': [str(self.record.pk)]}, 'purpose': '课程合成病例核对'})
        rows = self.rows(response)
        self.assertEqual(1, len(rows))
        self.assertEqual(str(self.record.pk), rows[0]['record_ref'])
        self.assertEqual(self.ref, rows[0]['encounter_ref'])
        self.assertIn('仅医生可见的合成诊断', response.content.decode('utf-8-sig'))
        self.assertIn('template-0.2', response.content.decode('utf-8-sig'))
        self.assertIn('clinical_versions', rows[0])
        event = AuditEvent.objects.get(action='export.clinical', outcome='success')
        self.assertEqual(self.doctor.pk, event.actor_id)
        self.assertEqual(self.a.pk, event.store_id)
        request = ExportRequest.objects.get(pk=event.target)
        self.assertEqual('课程合成病例核对', request.purpose)
        self.assertEqual(1, request.record_count)
        self.assertEqual('ready', request.state)
        self.assertNotIn('课程合成病例核对', json.dumps(list(AuditEvent.objects.values()), default=str, ensure_ascii=False))

    def test_clinical_export_serializes_rows_lazily(self):
        def consume(columns, rows):
            self.assertIs(iter(rows), rows)
            self.assertEqual([str(self.record.pk)], [row[0] for row in rows])
            return b'csv bytes'

        with patch('clinic.exports._csv_values', side_effect=consume):
            body, export_ref, count = clinical(self.doctor.pk, {
                'scope': {'record_refs': [str(self.record.pk)]}, 'purpose': '惰性序列化检查'})

        self.assertEqual((b'csv bytes', 1), (body, count))
        self.assertTrue(ExportRequest.objects.filter(pk=export_ref, record_count=1, row_count=1).exists())
        self.assertTrue(AuditTarget.objects.filter(target=self.record.pk, kind='record',
                                                   event__action='export.clinical').exists())

    def test_clinical_csv_is_built_outside_transaction_and_revocation_blocks_delivery(self):
        def revoke(columns, rows):
            self.assertFalse(connection.in_atomic_block)
            body = _csv_values(columns, rows)
            Account.objects.filter(pk=self.doctor.pk).update(is_active=False)
            return body

        with patch('clinic.exports._csv_values', side_effect=revoke):
            with self.assertRaises(DomainError) as error:
                clinical(self.doctor.pk, {'scope': {'record_refs': [str(self.record.pk)]}, 'purpose': '权限撤销'})
        self.assertEqual('AUTH_REQUIRED', error.exception.code)
        self.assertFalse(ExportRequest.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action='export.clinical', outcome='success').exists())

    def test_clinical_csv_uses_complete_read_snapshot_when_version_is_appended(self):
        def append_version(columns, rows):
            self.assertFalse(connection.in_atomic_block)
            body = _csv_values(columns, rows)
            ClinicalRevision.objects.create(record=self.record, version=2, diagnosis='后续更正', actor=self.doctor)
            ClinicalRecord.objects.filter(pk=self.record.pk).update(current_version=2)
            return body

        with patch('clinic.exports._csv_values', side_effect=append_version):
            body, _, count = clinical(self.doctor.pk, {
                'scope': {'record_refs': [str(self.record.pk)]}, 'purpose': '快照核对'})
        self.assertEqual(1, count)
        self.assertIn('仅医生可见的合成诊断', body.decode())
        self.assertNotIn('后续更正', body.decode())
        self.assertEqual(2, ClinicalRecord.objects.get(pk=self.record.pk).current_version)

    def test_field_meanings_are_cached_per_template_within_export(self):
        second_ref = self.create()['encounter_ref']
        second_record = self.record_fixture(second_ref)
        payload = {'scope': {'record_refs': [str(self.record.pk), str(second_record.pk)]},
                   'purpose': '模板字段含义缓存核对'}
        original_encode = _json_text
        field_meanings_encodes = 0

        def count_field_meanings(value):
            nonlocal field_meanings_encodes
            if value == {'diagnosis': 'text', 'examination': 'text'}:
                field_meanings_encodes += 1
            return original_encode(value)

        with patch('clinic.exports._json_text', side_effect=count_field_meanings):
            rows = self.rows(self.download(payload))

        self.assertEqual(2, len(rows))
        self.assertEqual(1, field_meanings_encodes)
        self.assertEqual(rows[0]['field_meanings'], rows[1]['field_meanings'])

    def test_clinical_json_encoding_preserves_compact_sorted_unicode_values(self):
        value = {'中文': {'z': [None, True, False, 1, 1.0, 1e-7],
                          'a': '逗号,引号"及换行\n'},
                 'keys': {'b': 2, 'a': 1}, 'wide_integer': 1 << 100}
        encoded = _json_text(value)
        expected = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        self.assertEqual(json.loads(expected), json.loads(encoded))
        self.assertTrue(encoded.startswith('{"keys":'))
        self.assertNotIn(': ', encoded)

    def test_deactivated_doctor_cannot_read_or_export(self):
        Account.objects.filter(pk=self.doctor.pk).update(is_active=False)
        with self.assertRaises(DomainError) as read_error:
            read(self.doctor.pk, clinical=True, ref=self.ref)
        self.assertEqual('AUTH_REQUIRED', read_error.exception.code)
        with self.assertRaises(DomainError) as export_error:
            clinical(self.doctor.pk, {'scope': {'record_refs': [str(self.record.pk)]}, 'purpose': '核对'})
        self.assertEqual('AUTH_REQUIRED', export_error.exception.code)
        self.assertFalse(ExportRequest.objects.exists())

    def test_one_unavailable_record_rejects_entire_explicit_set(self):
        other = self.create(payload={**self.payload(), 'doctor_ref': self.doctor2.pk})['encounter_ref']
        foreign_record = self.record_fixture(other)
        response = self.download({'scope': {'record_refs': [str(self.record.pk), str(foreign_record.pk)]}, 'purpose': '核对'})
        self.assertEqual((403, 'TARGET_UNAVAILABLE'),
                         (response.status_code, response.json()['error']['code']))
        self.assertNotIn('仅医生可见', response.content.decode())
        self.assertFalse(ExportRequest.objects.exists())
        event = AuditEvent.objects.get(actor=self.doctor, action='export.clinical',
                                       normalized_reason='TARGET_UNAVAILABLE')
        self.assertEqual(self.a.pk, event.store_id)
        self.assertEqual({self.record.pk, foreign_record.pk},
                         set(AuditTarget.objects.filter(event=event, kind='record').values_list('target', flat=True)))

    def test_cross_store_refusal_is_queryable_only_by_requesting_store(self):
        from governance.models import Account
        from .models import PetIdentity
        front_b = Account.objects.create_user(username='front-b', store=self.b, role='frontdesk')
        manager_b = Account.objects.create_user(username='manager-b', store=self.b, role='manager')
        encounter_b = Encounter.objects.create(store=self.b, pet=PetIdentity.objects.create(store=self.b),
                                               receptionist=front_b, doctor=self.foreign, template=self.template,
                                               species='猫', complaint='B店隐私')
        record_b = ClinicalRecord.objects.create(encounter=encounter_b, template=self.template)
        response = self.download({'scope': {'record_refs': [str(self.record.pk), str(record_b.pk)]}, 'purpose': '跨店核对'})
        self.assertEqual(403, response.status_code)
        self.client.force_login(self.manager)
        self.assertIn('export.clinical', [item['action'] for item in
                                         self.client.get('/api/audit', {'record_ref': str(record_b.pk)}).json()['items']])
        self.client.force_login(manager_b)
        self.assertNotIn('export.clinical', [item['action'] for item in
                                            self.client.get('/api/audit', {'record_ref': str(record_b.pk)}).json()['items']])

    def test_frontdesk_manager_and_empty_purpose_cannot_export(self):
        payload = {'scope': {'record_refs': [str(self.record.pk)]}, 'purpose': '核对'}
        for actor in [self.front, self.manager]:
            self.client.force_login(actor)
            self.assertEqual(403, self.download(payload).status_code)
        self.client.force_login(self.doctor)
        self.assertEqual(422, self.download({**payload, 'purpose': ''}).status_code)

    def test_manager_audit_can_find_export_by_record_without_seeing_content(self):
        response = self.download({'scope': {'record_refs': [str(self.record.pk)]}, 'purpose': '私密用途'})
        self.assertEqual(200, response.status_code)
        self.client.force_login(self.manager)
        result = self.client.get('/api/audit', {'record_ref': str(self.record.pk)})
        self.assertEqual(200, result.status_code)
        actions = [item['action'] for item in result.json()['items']]
        self.assertIn('export.clinical', actions)
        self.assertNotIn('私密用途', result.content.decode())
        self.assertTrue(AuditTarget.objects.filter(target=self.record.pk, kind='record',
                                                    event__action='export.clinical').exists())

    def test_generation_or_audit_failure_yields_no_csv(self):
        payload = {'scope': {'record_refs': [str(self.record.pk)]}, 'purpose': '核对'}
        with patch('clinic.exports.write_audit', side_effect=DatabaseError('synthetic audit outage')):
            response = self.download(payload)
        self.assertEqual(503, response.status_code)
        self.assertFalse(response['Content-Type'].startswith('text/csv'))
        with patch('clinic.exports._template', side_effect=KeyError('synthetic corrupt template')):
            response = self.download(payload)
        self.assertEqual(503, response.status_code)
        self.assertEqual('EXPORT_GENERATION_UNAVAILABLE', response.json()['error']['code'])
        self.assertFalse(ExportRequest.objects.exists())
        ClinicalRevision.objects.filter(record=self.record).update(diagnosis='')
        response = self.download(payload)
        self.assertEqual((503, 'RECORD_UNAVAILABLE'),
                         (response.status_code, response.json()['error']['code']))
        self.assertFalse(response['Content-Type'].startswith('text/csv'))
        self.assertFalse(ExportRequest.objects.exists())
        self.assertTrue(AuditEvent.objects.filter(actor=self.doctor, store=self.a,
                                                  action='export.clinical',
                                                  normalized_reason='RECORD_UNAVAILABLE').exists())

    def test_time_range_is_complete_not_a_list_page(self):
        from datetime import timedelta
        from django.utils import timezone
        start = timezone.now().replace(microsecond=0)
        second = self.create()['encounter_ref']
        second_record = self.record_fixture(second)
        Encounter.objects.filter(pk=self.ref).update(created_at=start)
        Encounter.objects.filter(pk=second).update(created_at=start + timedelta(hours=1))
        self.client.force_login(self.doctor)
        payload = {'scope': {'time_range': {'from': start.isoformat(),
                                            'to': (start + timedelta(hours=1)).isoformat()}}, 'purpose': '核对时间'}
        response = self.download(payload)
        self.assertEqual([str(self.record.pk)], [r['record_ref'] for r in self.rows(response)])
        payload['scope']['time_range']['to'] = (start + timedelta(hours=2)).isoformat()
        self.assertEqual({str(self.record.pk), str(second_record.pk)},
                         {r['record_ref'] for r in self.rows(self.download(payload))})

    def test_empty_range_and_csv_special_characters_are_reversible(self):
        from datetime import timedelta
        from django.utils import timezone
        now = timezone.now()
        empty = self.download({'scope': {'time_range': {'from': (now - timedelta(days=3)).isoformat(),
                                                       'to': (now - timedelta(days=2)).isoformat()}}, 'purpose': '空范围'})
        self.assertEqual([], self.rows(empty))
        self.assertEqual('0', empty['X-Record-Count'])
        special = '中文,引号"及\n换行'
        ClinicalRevision.objects.filter(record=self.record).update(diagnosis=special)
        response = self.download({'scope': {'record_refs': [str(self.record.pk)]}, 'purpose': '特殊字符'})
        self.assertEqual(special, self.rows(response)[0]['diagnosis'])

    def test_malformed_scope_and_time_range_do_not_create_export(self):
        for payload in [
            {'scope': {'record_refs': [str(self.record.pk), str(self.record.pk)]}, 'purpose': '核对'},
            {'scope': {'record_refs': [str(self.record.pk)], 'time_range': {}}, 'purpose': '核对'},
            {'scope': {'time_range': {'from': 2, 'to': 3}}, 'purpose': '核对'},
            {'scope': {'record_refs': [str(self.record.pk)]}, 'purpose': '核对', 'store_ref': str(self.b.pk)},
        ]:
            self.assertEqual(422, self.download(payload).status_code)
        self.assertEqual(0, ExportRequest.objects.count())

    def test_corrections_medication_and_adopted_only_remain_reconstructable(self):
        from .models import MedicationEntry
        original = ClinicalRevision.objects.get(record=self.record)
        newer = ClinicalRevision.objects.create(record=self.record, version=2, diagnosis='更正后诊断',
                                                 examination='更正检查', actor=self.doctor, reason='合成更正理由')
        ClinicalRecord.objects.filter(pk=self.record.pk).update(current_version=2)
        check = ClinicalCheck.objects.create(encounter_id=self.ref, record=self.record, actor=self.doctor,
                                            mode='medication', template=self.template, policy=self.template,
                                            policy_digest=self.template.digest, status='accepted', checks=[])
        original_med = MedicationEntry.objects.create(record=self.record, evaluation=check, drug='合成药', amount='1',
                                                      unit='片', actor=self.doctor)
        check2 = ClinicalCheck.objects.create(encounter_id=self.ref, record=self.record, actor=self.doctor,
                                            mode='medication', template=self.template, policy=self.template,
                                            policy_digest=self.template.digest, status='accepted', checks=[])
        correction = MedicationEntry.objects.create(record=self.record, original=original_med, evaluation=check2,
                                                     drug='合成药', amount='2', unit='片', actor=self.doctor,
                                                     reason='合成用药更正')
        accepted = Suggestion.objects.create(encounter_id=self.ref, source_version='demo', sample_key='accept',
                                             source={'type': 'demo', 'version': 'demo', 'label': '演示'},
                                             content={'diagnosis': '原始演示建议'})
        declined = Suggestion.objects.create(encounter_id=self.ref, source_version='demo', sample_key='decline',
                                             source=accepted.source, content={'diagnosis': '未采纳秘密'})
        SuggestionDecision.objects.create(suggestion=accepted, outcome='adopted', actor=self.doctor,
                                          adopted_content={'diagnosis': '医生采纳内容'}, record=self.record, version=newer)
        SuggestionDecision.objects.create(suggestion=declined, outcome='declined', actor=self.doctor)
        Encounter.objects.filter(pk=self.ref).update(error_mark=True)
        response = self.download({'scope': {'record_refs': [str(self.record.pk)]}, 'purpose': '核对链'})
        row = self.rows(response)[0]
        self.assertEqual('true', row['error_mark'])
        self.assertEqual([str(original.pk), str(newer.pk)],
                         [v['version_ref'] for v in json.loads(row['clinical_versions'])])
        chain = json.loads(row['medication_chain'])
        self.assertEqual(2, len(chain))
        self.assertEqual(str(original_med.pk), chain[1]['original_entry_ref'])
        self.assertEqual(str(correction.pk), chain[1]['entry_ref'])
        self.assertEqual(str(accepted.pk), json.loads(row['adopted_content'])[0]['suggestion_ref'])
        self.assertNotIn('未采纳秘密', response.content.decode('utf-8-sig'))


class GuardExportTests(ClinicFixture):
    detail_url = '/api/exports/alert-hits'
    summary_url = '/api/exports/alert-summary'

    def setUp(self):
        super().setUp()
        self.ref = self.create()['encounter_ref']
        diagnosis(self.doctor.pk, 'record', str(uuid.uuid4()), {}, self.ref)

    def download(self, url, payload):
        return self.client.post(url, json.dumps(payload), content_type='application/json')

    def parsed(self, response):
        self.assertEqual(200, response.status_code, response.content[:200])
        return list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))

    def test_doctor_detail_contains_only_authorized_hit_metadata(self):
        self.client.force_login(self.doctor)
        response = self.download(self.detail_url, {'scope': {'encounter_refs': [self.ref]}})
        rows = self.parsed(response)
        self.assertEqual(1, len(rows))
        self.assertEqual('SR-02', rows[0]['rule_ref'])
        self.assertEqual('unhandled', rows[0]['handling_state'])
        self.assertNotIn('value', rows[0])
        self.assertNotIn('合成主诉', response.content.decode('utf-8-sig'))
        self.assertEqual(1, ExportRequest.objects.filter(permission='export.guard_detail').count())

    def test_manager_summary_has_counts_without_case_identifiers(self):
        self.client.force_login(self.manager)
        response = self.download(self.summary_url, {'group_by': 'rule'})
        rows = self.parsed(response)
        self.assertEqual(1, len(rows))
        self.assertEqual('SR-02', rows[0]['rule_ref'])
        self.assertEqual('1', rows[0]['hit_count'])
        self.assertNotIn(self.ref, response.content.decode('utf-8-sig'))
        self.assertNotIn('record_ref', rows[0])
        self.assertEqual(1, ExportRequest.objects.filter(permission='export.guard_summary').count())

    def test_detail_projection_skips_clinical_json_and_preserves_missing_alert(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.client.force_login(self.doctor)
        # An absent alert remains an unhandled hit in the CSV.
        alert = Alert.objects.get()
        alert.alertoutbox.delete()
        alert.delete()
        with CaptureQueriesContext(connection) as captured:
            response = self.download(self.detail_url, {'scope': {'encounter_refs': [self.ref]}})
        rows = self.parsed(response)
        self.assertEqual(('unhandled', '', '', ''), tuple(rows[0][field] for field in (
            'handling_state', 'handler_ref', 'handled_at', 'handling_result')))
        selected = [q['sql'] for q in captured.captured_queries
                    if q['sql'].lstrip().upper().startswith('SELECT') and
                    'clinic_guardhit' in q['sql']]
        self.assertEqual(1, len(selected))
        projection = selected[0].split(' FROM ')[0]
        for unused in ('checks', 'policy_digest', 'value', 'original_version_id'):
            self.assertNotIn(unused, projection)

    def test_handling_state_summary_combines_different_handling_times(self):
        from django.utils import timezone
        from datetime import timedelta
        second = self.create()['encounter_ref']
        diagnosis(self.doctor.pk, 'record', str(uuid.uuid4()), {}, second)
        alerts = list(Alert.objects.order_by('id'))
        Alert.objects.filter(pk=alerts[0].pk).update(handler=self.doctor, handled_at=timezone.now(), handling_result='隐私理由A')
        Alert.objects.filter(pk=alerts[1].pk).update(handler=self.doctor, handled_at=timezone.now()+timedelta(seconds=1), handling_result='隐私理由B')
        self.client.force_login(self.manager)
        response = self.download(self.summary_url, {'group_by': 'handling_state'})
        rows = self.parsed(response)
        self.assertEqual(1, len(rows))
        self.assertEqual('handled', rows[0]['handling_state'])
        self.assertEqual('2', rows[0]['alert_count'])
        self.assertNotIn('隐私理由', response.content.decode('utf-8-sig'))

    def test_roles_and_case_scope_are_enforced(self):
        self.client.force_login(self.front)
        self.assertEqual(403, self.download(self.detail_url, {}).status_code)
        self.assertEqual(403, self.download(self.summary_url, {}).status_code)
        self.client.force_login(self.manager)
        self.assertEqual(422, self.download(self.summary_url, {'scope': {'encounter_refs': [self.ref]}}).status_code)
        self.client.force_login(self.doctor)
        self.assertEqual(403, self.download(self.summary_url, {}).status_code)
        self.assertEqual(403, self.download(self.detail_url, {'scope': {'encounter_refs': [str(uuid.uuid4())]}}).status_code)

    def test_guard_export_business_errors_are_json_audited_and_have_no_file(self):
        self.client.force_login(self.doctor)
        for payload, status, code in [
            ({'scope': {'encounter_refs': [str(uuid.uuid4())]}}, 403, 'TARGET_UNAVAILABLE'),
            ({'handling_state': 'unknown'}, 422, 'INPUT_INVALID'),
            ({'time_range': {'from': '2026-10-24', 'to': '2026-10-23'}}, 422, 'INPUT_INVALID'),
        ]:
            response = self.download(self.detail_url, payload)
            self.assertEqual((status, code), (response.status_code, response.json()['error']['code']))
            self.assertFalse(response['Content-Type'].startswith('text/csv'))
            self.assertTrue(AuditEvent.objects.filter(actor=self.doctor, store=self.a,
                                                      action='export.guard_detail', normalized_reason=code).exists())
        self.client.force_login(self.manager)
        for payload, code in [
            ({'scope': {'encounter_refs': [self.ref]}}, 'INPUT_FIELDS_INVALID'),
            ({'group_by': 'record'}, 'INPUT_INVALID'),
        ]:
            response = self.download(self.summary_url, payload)
            self.assertEqual((422, code), (response.status_code, response.json()['error']['code']))
            self.assertFalse(response['Content-Type'].startswith('text/csv'))
            self.assertTrue(AuditEvent.objects.filter(actor=self.manager, store=self.a,
                                                      action='export.guard_summary', normalized_reason=code).exists())
        self.assertFalse(ExportRequest.objects.exists())

    def test_audit_failure_prevents_csv(self):
        self.client.force_login(self.manager)
        with patch('clinic.exports.write_audit', side_effect=DatabaseError('synthetic')):
            response = self.download(self.summary_url, {})
        self.assertEqual(503, response.status_code)
        self.assertFalse(response['Content-Type'].startswith('text/csv'))

    def test_guard_csv_is_built_outside_transaction_and_revocation_blocks_delivery(self):
        def revoke(columns, rows):
            self.assertFalse(connection.in_atomic_block)
            body = _csv(columns, rows)
            Account.objects.filter(pk=self.manager.pk).update(is_active=False)
            return body

        with patch('clinic.exports._csv', side_effect=revoke):
            with self.assertRaises(DomainError) as error:
                guard(self.manager.pk, SUMMARY, {})
        self.assertEqual('AUTH_REQUIRED', error.exception.code)
        self.assertFalse(ExportRequest.objects.exists())

    def test_filters_and_repeated_downloads_are_individually_audited(self):
        from datetime import timedelta
        from django.utils import timezone
        self.client.force_login(self.doctor)
        hit = Alert.objects.get().hit
        at = hit.evaluation.created_at
        payload = {'scope': {'encounter_refs': [self.ref]},
                   'time_range': {'from': (at - timedelta(seconds=1)).isoformat(),
                                  'to': (at + timedelta(seconds=1)).isoformat()},
                   'rule_ref': hit.rule_ref, 'handling_state': 'unhandled'}
        first = self.download(self.detail_url, payload)
        second = self.download(self.detail_url, payload)
        self.assertEqual(1, len(self.parsed(first)))
        self.assertEqual(1, len(self.parsed(second)))
        self.assertNotEqual(first['X-Export-Ref'], second['X-Export-Ref'])
        self.assertEqual(2, AuditEvent.objects.filter(action='export.guard_detail', outcome='success').count())
        self.assertEqual([], self.parsed(self.download(self.detail_url, {**payload, 'rule_ref': 'not-this-rule'})))
        self.client.force_login(self.manager)
        self.assertEqual([], self.parsed(self.download(self.summary_url, {'handling_state': 'handled'})))

    def test_summary_excludes_another_store_and_rejects_clinical_filters(self):
        from governance.models import Store
        from .models import ClinicalCheck, GuardHit, PetIdentity
        other_store = Store.objects.create(name='第三店')
        other_front = type(self.front).objects.create_user(username='third-front', store=other_store, role='frontdesk')
        other_doctor = type(self.doctor).objects.create_user(username='third-doctor', store=other_store, role='doctor')
        other = Encounter.objects.create(store=other_store, pet=PetIdentity.objects.create(store=other_store),
                                         receptionist=other_front, doctor=other_doctor, template=self.template,
                                         species='猫', complaint='第三店隐私')
        check = ClinicalCheck.objects.create(encounter=other, actor=other_doctor, mode='record',
                                             template=self.template, policy=self.template,
                                             policy_digest=self.template.digest, status='rejected', checks=[])
        GuardHit.objects.create(evaluation=check, rule_ref='OTHER-STORE', rule_version='v1', field='secret', value='第三店隐私')
        self.client.force_login(self.manager)
        response = self.download(self.summary_url, {})
        self.assertNotIn('OTHER-STORE', response.content.decode())
        self.assertNotIn('第三店隐私', response.content.decode())
        for forbidden in ['record_ref', 'encounter_ref', 'purpose', 'doctor_ref']:
            self.assertEqual(422, self.download(self.summary_url, {forbidden: 'secret'}).status_code)
