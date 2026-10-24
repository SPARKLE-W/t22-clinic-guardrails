import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from django.db import DatabaseError, close_old_connections
from django.test import Client
from governance.models import AuditEvent, Operation, PolicyHead
from .models import ClinicalRecord, ClinicalRevision, Encounter
from .services import mutate
from .tests import ClinicFixture


class ClinicalWriteTests(ClinicFixture):
    def post(self, ref, payload, mode='record', actor=None, key=None):
        client = Client()
        client.force_login(actor or self.doctor)
        suffix = 'diagnosis' if mode == 'record' else 'diagnosis/corrections'
        return client.post('/api/clinical/encounters/' + ref + '/' + suffix, payload,
                           content_type='application/json', HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))

    def test_assigned_doctor_can_record_diagnosis(self):
        ref = self.create()['encounter_ref']
        response = self.post(ref, {'diagnosis': '合成诊断', 'examination': '合成检查'})
        self.assertEqual(200, response.status_code)
        self.assertEqual('合成诊断', response.json()['diagnosis'])

    def test_optional_template_fields_preserve_types_versions_and_export(self):
        import csv
        import io
        import json
        from .exports import CLINICAL_COLUMNS
        extra = [{'id': 'x_followup_note', 'type': 'text', 'required': False, 'visible_role': 'doctor'},
                 {'id': 'x_visit_count', 'type': 'integer', 'required': False, 'visible_role': 'doctor'},
                 {'id': 'x_is_urgent', 'type': 'boolean', 'required': False, 'visible_role': 'doctor'}]
        template = self.template_fixture('template-extra-1', extra_fields=extra)
        PolicyHead.objects.filter(store=self.a).update(candidate=template, generation=2)
        ref = self.create()['encounter_ref']
        first = self.post(ref, {'diagnosis': '初诊', 'extra_fields': {
            'x_followup_note': '七日后复诊', 'x_visit_count': 1, 'x_is_urgent': False}})
        self.assertEqual(200, first.status_code)
        self.assertEqual({'x_followup_note': '七日后复诊', 'x_visit_count': 1, 'x_is_urgent': False},
                         first.json()['extra_fields'])
        record = ClinicalRecord.objects.get(encounter_id=ref)
        original = ClinicalRevision.objects.get(record=record, version=1)
        self.assertEqual('template-extra-1', first.json()['template_version_ref'])
        changed = {'original_content_ref': str(record.pk), 'original_version_ref': str(original.pk),
                   'reason': '核对后更正', 'extra_fields': {'x_followup_note': '十日后复诊', 'x_is_urgent': None}}
        second = self.post(ref, changed, mode='correct')
        self.assertEqual(200, second.status_code)
        self.assertEqual({'x_followup_note': '十日后复诊', 'x_visit_count': 1}, second.json()['extra_fields'])
        original.refresh_from_db()
        self.assertEqual(False, original.extra_fields['x_is_urgent'])
        self.assertEqual('七日后复诊', original.extra_fields['x_followup_note'])
        self.assertEqual(2, len(second.json()['clinical_versions']))
        self.assertEqual(original.extra_fields, second.json()['clinical_versions'][0]['extra_fields'])

        self.assertEqual(422, self.post(ref, {'original_content_ref': str(record.pk),
            'original_version_ref': second.json()['clinical_versions'][-1]['version_ref'],
            'reason': '非法字段', 'extra_fields': {'x_unknown': '不得存入'}}, mode='correct').status_code)
        self.assertEqual(422, self.post(ref, {'original_content_ref': str(record.pk),
            'original_version_ref': second.json()['clinical_versions'][-1]['version_ref'],
            'reason': '类型不符', 'extra_fields': {'x_visit_count': True}}, mode='correct').status_code)
        self.assertEqual(2, ClinicalRevision.objects.filter(record=record).count())

        from .exports import clinical
        body, _, count = clinical(self.doctor.pk, {'scope': {'record_refs': [str(record.pk)]}, 'purpose': '合成模板回归'})
        self.assertEqual(1, count)
        row = next(csv.DictReader(io.StringIO(body.decode('utf-8'))))
        self.assertIn('extra_fields', CLINICAL_COLUMNS)
        self.assertEqual(second.json()['extra_fields'], json.loads(row['extra_fields']))
        self.assertEqual(2, len(json.loads(row['clinical_versions'])))

    def test_correction_keeps_original_and_rejects_stale_or_forged_parent(self):
        ref = self.create()['encounter_ref']
        first = self.post(ref, {'diagnosis': '初版', 'examination': '原检查'})
        self.assertEqual(200, first.status_code)
        record = ClinicalRecord.objects.get(encounter_id=ref)
        original = ClinicalRevision.objects.get(record=record)
        values = {'original_content_ref': str(record.pk), 'original_version_ref': str(original.pk), 'diagnosis': '更正诊断', 'reason': '核对后更正'}
        wrong = self.post(ref, {**values, 'original_content_ref': str(uuid.uuid4())}, mode='correct')
        self.assertEqual(409, wrong.status_code)
        saved = self.post(ref, values, mode='correct')
        self.assertEqual(200, saved.status_code)
        self.assertEqual('原检查', saved.json()['examination'])
        self.assertEqual(409, self.post(ref, values, mode='correct').status_code)
        original.refresh_from_db()
        self.assertEqual('初版', original.diagnosis)
        self.assertEqual(2, record.clinicalrevision_set.count())
        from .models import ClinicalCheck
        check = ClinicalCheck.objects.filter(mode='correct').get()
        self.assertTrue(hasattr(check, 'original_version_id'))
        self.assertEqual(original.pk, check.original_version_id)
        self.assertEqual(2, check.result_version.version)

    def test_role_scope_and_server_owned_input_cannot_bypass_entry(self):
        ref = self.create()['encounter_ref']
        for actor in [self.front, self.manager, self.doctor2, self.foreign]:
            with self.subTest(actor=actor.username):
                self.assertEqual(403, self.post(ref, {'diagnosis': '不得写入'}, actor=actor).status_code)
        self.assertEqual(422, self.post(ref, {'diagnosis': '输入', 'actor_ref': self.doctor.pk}).status_code)
        self.assertFalse(ClinicalRecord.objects.exists())
        event = AuditEvent.objects.filter(actor=self.foreign).latest('created_at')
        self.assertEqual(self.b.pk, event.store_id)

    def test_missing_diagnosis_is_durable_guard_hit_and_retries_do_not_duplicate(self):
        ref = self.create()['encounter_ref']
        key = str(uuid.uuid4())
        first = self.post(ref, {'examination': '合成检查'}, key=key)
        self.assertEqual(422, first.status_code)
        self.assertEqual('GUARD_BLOCKED', first.json()['error']['code'])
        from .models import Alert, AlertOutbox, ClinicalCheck, GuardHit
        self.assertEqual(1, ClinicalCheck.objects.count())
        self.assertEqual('SR-02', GuardHit.objects.get().rule_ref)
        self.assertEqual('diagnosis', GuardHit.objects.get().field)
        self.assertEqual(1, Alert.objects.count())
        self.assertEqual('pending', AlertOutbox.objects.get().state)
        self.assertEqual(first.json(), self.post(ref, {'examination': '合成检查'}, key=key).json())
        self.assertEqual(1, Alert.objects.count())
        self.assertEqual(409, self.post(ref, {'diagnosis': '变更了内容'}, key=key).status_code)
        self.assertEqual(422, self.post(ref, {'examination': '合成检查'}).status_code)
        self.assertEqual(2, Alert.objects.count())
        self.assertFalse(ClinicalRecord.objects.exists())

    def test_format_error_and_unauthorized_request_do_not_create_clinical_alert(self):
        ref = self.create()['encounter_ref']
        self.assertEqual(422, self.post(ref, {'diagnosis': []}).status_code)
        self.assertEqual(403, self.post(ref, {}, actor=self.front).status_code)
        from .models import Alert
        self.assertFalse(Alert.objects.exists())

    def test_old_template_is_pinned_but_current_rule_policy_is_used(self):
        ref = self.create()['encounter_ref']
        newer = self.template_fixture('template-0.3')
        PolicyHead.objects.filter(store=self.a).update(candidate=newer, generation=2)
        response = self.post(ref, {'diagnosis': '按原模板解释'})
        self.assertEqual(200, response.status_code)
        from .models import ClinicalCheck
        check = ClinicalCheck.objects.get()
        self.assertEqual(newer.pk, check.policy_id)
        self.assertEqual(self.template.pk, check.template_id)
        self.assertEqual('template-0.2', response.json()['template_version_ref'])

    def test_missing_or_tampered_policy_fails_closed_without_fake_alert(self):
        ref = self.create()['encounter_ref']
        PolicyHead.objects.filter(store=self.a).update(candidate=None)
        self.assertEqual(503, self.post(ref, {'diagnosis': '不能提交'}).status_code)
        PolicyHead.objects.filter(store=self.a).update(candidate=self.template)
        envelope = self.template.envelope
        envelope['payload']['demand']['safety_redlines'] = []
        self.template.envelope = envelope
        self.template.save(update_fields=['envelope'])
        self.assertEqual(503, self.post(ref, {'diagnosis': '不能提交'}).status_code)
        from .models import Alert
        self.assertFalse(Alert.objects.exists())
        self.assertFalse(ClinicalRecord.objects.exists())

    def test_misregistration_blocks_new_record_but_allows_existing_correction(self):
        ref = self.create()['encounter_ref']
        record = self.record_fixture(ref)
        original = record.clinicalrevision_set.get()
        mutate(self.front.pk, 'mark', str(uuid.uuid4()), {'reason': '合成误建'}, ref)
        self.assertEqual(409, self.post(ref, {'diagnosis': '不能新增'}).status_code)
        response = self.post(ref, {'diagnosis': '已有内容纠错', 'original_content_ref': str(record.pk), 'original_version_ref': str(original.pk), 'reason': '误建纠错'}, mode='correct')
        self.assertEqual(200, response.status_code)
        other = self.create()['encounter_ref']
        mutate(self.front.pk, 'mark', str(uuid.uuid4()), {'reason': '合成误建'}, other)
        self.assertEqual(409, self.post(other, {'diagnosis': '不能新增'}).status_code)
        self.assertEqual(409, self.post(other, {'diagnosis': '不能借更正新增', 'original_content_ref': str(uuid.uuid4()), 'original_version_ref': str(uuid.uuid4()), 'reason': '测试'}, mode='correct').status_code)

    def test_success_replay_checks_current_authorization(self):
        ref = self.create()['encounter_ref']
        key = str(uuid.uuid4())
        values = {'diagnosis': '合成诊断'}
        first = self.post(ref, values, key=key)
        self.assertEqual(200, first.status_code)
        self.assertEqual(first.json(), self.post(ref, values, key=key).json())
        self.assertEqual(1, ClinicalRevision.objects.count())
        Encounter.objects.filter(pk=ref).update(doctor=self.doctor2)
        self.assertEqual(403, self.post(ref, values, key=key).status_code)

    def test_success_audit_failure_rolls_back_all_business_and_check_rows(self):
        ref = self.create()['encounter_ref']
        with patch('clinic.writes.write_audit', side_effect=DatabaseError('injected')):
            self.assertEqual(503, self.post(ref, {'diagnosis': '必须回滚'}).status_code)
        from .models import ClinicalCheck
        self.assertFalse(ClinicalRecord.objects.exists())
        self.assertFalse(ClinicalRevision.objects.exists())
        self.assertFalse(ClinicalCheck.objects.exists())

    def test_guard_outbox_failure_does_not_claim_durable_alert(self):
        ref = self.create()['encounter_ref']
        from .models import Alert, AlertOutbox, ClinicalCheck
        with patch.object(AlertOutbox.objects, 'create', side_effect=DatabaseError('injected')):
            response = self.post(ref, {})
        self.assertEqual(503, response.status_code)
        self.assertEqual('PERSISTENCE_UNCONFIRMED', response.json()['error']['code'])
        self.assertFalse(Alert.objects.exists())
        self.assertFalse(ClinicalCheck.objects.exists())
        self.assertFalse(Operation.objects.filter(permission='clinical.diagnosis.write').exists())

    def test_alert_read_and_resolution_are_doctor_scoped_and_not_delivery(self):
        ref = self.create()['encounter_ref']
        self.assertEqual(422, self.post(ref, {}).status_code)
        client = Client()
        client.force_login(self.doctor)
        response = client.get('/api/clinical/alerts')
        self.assertEqual(200, response.status_code)
        item = response.json()['items'][0]
        self.assertEqual('unhandled', item['handling_state'])
        self.assertIsNone(item['handler_ref'])
        self.assertNotIn('value', item)
        for actor in [self.front, self.manager, self.doctor2, self.foreign]:
            client.force_login(actor)
            denied = client.post('/api/clinical/alerts/' + item['alert_ref'] + '/resolution', {'alert_ref': item['alert_ref'], 'handling_result': '越权'}, content_type='application/json', HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
            self.assertEqual(403, denied.status_code)
        client.force_login(self.doctor)
        mutate(self.front.pk, 'mark', str(uuid.uuid4()), {'reason': '合成误建'}, ref)
        key = str(uuid.uuid4())
        url = '/api/clinical/alerts/' + item['alert_ref'] + '/resolution'
        body = {'alert_ref': item['alert_ref'], 'handling_result': '核对缺项，后续另行补全'}
        resolved = client.post(url, body, content_type='application/json', HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(200, resolved.status_code)
        self.assertEqual('handled', resolved.json()['handling_state'])
        self.assertEqual(self.doctor.pk, resolved.json()['handler_ref'])
        self.assertEqual(resolved.json(), client.post(url, body, content_type='application/json', HTTP_IDEMPOTENCY_KEY=key).json())
        from .models import AlertOutbox
        self.assertEqual('pending', AlertOutbox.objects.get().state)

    def test_cross_store_alert_refusal_keeps_target_and_requester_store(self):
        ref = self.create()['encounter_ref']
        self.assertEqual(422, self.post(ref, {}).status_code)
        from .models import Alert, AuditTarget
        alert = Alert.objects.get()
        client = Client()
        client.force_login(self.foreign)
        response = client.post('/api/clinical/alerts/' + str(alert.pk) + '/resolution',
                               {'alert_ref': str(alert.pk), 'handling_result': '不应允许'},
                               content_type='application/json', HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        self.assertEqual(403, response.status_code)
        event = AuditEvent.objects.filter(actor=self.foreign).latest('created_at')
        self.assertEqual(self.b.pk, event.store_id)
        self.assertEqual(str(ref), str(event.target))
        self.assertTrue(AuditTarget.objects.filter(event=event, target=alert.pk, kind='alert').exists())

    def test_additional_rule_uses_server_facts_and_preserves_distinct_rule_hits(self):
        rule = {'id': 'EXT-MISSING', 'version': '1', 'checker': 'referenced_template',
                'phases': ['clinical_change'], 'source': ['SR-02'], 'depends_on': ['SR-02'],
                'when': {'op': 'all', 'args': [{'op': 'eq', 'fact': 'principal.role', 'value': 'doctor'},
                                              {'op': 'eq', 'fact': 'template.required_fields_valid', 'value': False}]}}
        ref = self.create()['encounter_ref']
        policy = self.template_fixture('template-rules', [rule])
        PolicyHead.objects.filter(store=self.a).update(candidate=policy, generation=2)
        response = self.post(ref, {})
        self.assertEqual(422, response.status_code)
        from .models import GuardHit
        self.assertEqual({'SR-02', 'EXT-MISSING'}, set(GuardHit.objects.values_list('rule_ref', flat=True)))
        self.assertEqual(200, self.post(ref, {'diagnosis': '合成诊断'}).status_code)

    def test_condition_types_and_unavailable_facts_fail_closed(self):
        from .guards import condition
        from governance.services import DomainError
        self.assertFalse(condition({'op': 'eq', 'fact': 'x', 'value': 1}, {'x': True}))
        self.assertTrue(condition({'op': 'in', 'fact': 'x', 'values': ['doctor', 'manager']}, {'x': 'doctor'}))
        with self.assertRaises(DomainError):
            condition({'op': 'any', 'args': [{'op': 'present', 'fact': 'known'}, {'op': 'present', 'fact': 'missing'}]}, {'known': True})

    def test_concurrent_same_operation_and_stale_corrections_do_not_duplicate(self):
        from .writes import diagnosis
        ref = self.create()['encounter_ref']

        def concurrent(mode, keys, payloads):
            barrier = Barrier(2)
            def task(index):
                close_old_connections()
                try:
                    barrier.wait(timeout=10)
                    try:
                        return ('ok', diagnosis(self.doctor.pk, mode, keys[index], payloads[index], ref))
                    except Exception as error:
                        return (getattr(error, 'code', type(error).__name__), None)
                finally:
                    close_old_connections()
            with ThreadPoolExecutor(max_workers=2) as pool:
                return list(pool.map(task, [0, 1]))

        key = str(uuid.uuid4())
        results = concurrent('record', [key, key], [{'diagnosis': '初版'}] * 2)
        self.assertEqual(['ok', 'ok'], [row[0] for row in results])
        self.assertEqual(results[0][1], results[1][1])
        record = ClinicalRecord.objects.get(encounter_id=ref)
        original = record.clinicalrevision_set.get()
        values = {'original_content_ref': str(record.pk), 'original_version_ref': str(original.pk), 'diagnosis': '更正', 'reason': '核对'}
        results = concurrent('correct', [str(uuid.uuid4()), str(uuid.uuid4())], [values, values])
        self.assertEqual(['STATE_OR_VERSION_CONFLICT', 'ok'], sorted(row[0] for row in results))
        self.assertEqual(2, record.clinicalrevision_set.count())

    def test_alert_audit_failure_does_not_release_or_resolve_sensitive_data(self):
        ref = self.create()['encounter_ref']
        self.assertEqual(422, self.post(ref, {}).status_code)
        from .models import Alert
        alert = Alert.objects.get()
        client = Client()
        client.force_login(self.doctor)
        with patch('clinic.writes.write_audit', side_effect=DatabaseError('injected')):
            response = client.get('/api/clinical/alerts')
            self.assertEqual(503, response.status_code)
            self.assertNotIn('items', response.json())
            response = client.post('/api/clinical/alerts/' + str(alert.pk) + '/resolution',
                                   {'alert_ref': str(alert.pk), 'handling_result': '必须回滚'},
                                   content_type='application/json', HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
            self.assertEqual(503, response.status_code)
        alert.refresh_from_db()
        self.assertIsNone(alert.handled_at)

    def test_record_filtered_alert_refusal_keeps_real_parent_and_record_index(self):
        ref = self.create()['encounter_ref']
        record = self.record_fixture(ref)
        from .models import AuditTarget
        client = Client()
        for actor in [self.foreign, self.front, self.manager]:
            client.force_login(actor)
            response = client.get('/api/clinical/alerts', {'record_ref': str(record.pk)})
            self.assertEqual(403, response.status_code)
            event = AuditEvent.objects.filter(actor=actor, action='clinical.alert.handle').latest('created_at')
            self.assertEqual(actor.store_id, event.store_id)
            self.assertEqual(str(ref), str(event.target))
            self.assertTrue(AuditTarget.objects.filter(event=event, target=record.pk, kind='record').exists())

    def test_alert_opens_its_actual_encounter_even_before_first_record(self):
        from .models import Alert
        for index in range(2):
            ref = self.create()['encounter_ref']
            self.assertEqual(422, self.post(ref, {}).status_code)
            alert = Alert.objects.get(hit__evaluation__encounter_id=ref)
            client = Client()
            client.force_login(self.doctor)
            url = '/api/clinical/alerts/' + str(alert.pk) + '/encounter'
            response = client.get(url)
            self.assertEqual(200, response.status_code)
            self.assertEqual(ref, response.json()['reception']['encounter_ref'])
            self.assertIsNone(response.json()['clinical'])
            for actor in [self.front, self.manager, self.foreign, self.doctor2]:
                client.force_login(actor)
                self.assertEqual(403, client.get(url).status_code)
                event = AuditEvent.objects.filter(actor=actor).latest('created_at')
                self.assertEqual('clinical.read', event.action)
                self.assertEqual(str(ref), str(event.target))
                self.assertEqual(actor.store_id, event.store_id)
