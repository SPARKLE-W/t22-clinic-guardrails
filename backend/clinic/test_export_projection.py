import csv
import io
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.core.management import call_command
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from governance.models import Operation, Review
from governance.services import DomainError
from .exports import clinical, _clinical_id_selection, _record_values
from .export_projection import rebuild_projections
from .models import (AuditTarget, ClinicalExportProjection, ClinicalRecord, ClinicalRevision,
                     Encounter, ExportRequest, Suggestion)
from .tests import ClinicFixture
from .writes import diagnosis
from .medications import mutate as medication
from .suggestions import mutate as suggestion
from .services import read


class ExportProjectionTests(ClinicFixture):
    def setUp(self):
        super().setUp()
        self.ref = self.create()['encounter_ref']
        diagnosis(self.doctor.pk, 'record', str(uuid.uuid4()),
                  {'diagnosis': '中文,初诊"\n', 'examination': '合成检查'}, self.ref)
        self.record = ClinicalRecord.objects.get(encounter_id=self.ref)

    def body(self, source=False):
        payload = {'scope': {'record_refs': [str(self.record.pk)]}, 'purpose': '投影一致性'}
        if source:
            with patch('clinic.exports._projection_prefix', return_value=None):
                return clinical(self.doctor.pk, payload)[0]
        return clinical(self.doctor.pk, payload)[0]

    def row(self):
        body = self.body()
        self.assertEqual(self.body(source=True), body)
        with patch('clinic.services._projected_clinical', return_value=None):
            expected = read(self.doctor.pk, clinical=True, ref=self.ref)
        self.assertEqual(expected, read(self.doctor.pk, clinical=True, ref=self.ref))
        return next(csv.DictReader(io.StringIO(body.decode('utf-8'))))

    def correct(self, diagnosis_value='更正后诊断', key=None):
        self.record.refresh_from_db()
        current = ClinicalRevision.objects.get(record=self.record, version=self.record.current_version)
        return diagnosis(self.doctor.pk, 'correct', key or str(uuid.uuid4()), {
            'diagnosis': diagnosis_value, 'original_content_ref': str(self.record.pk),
            'original_version_ref': str(current.pk), 'reason': '合成更正'}, self.ref)

    def test_single_read_projection_matches_history_and_reduces_queries(self):
        with patch('clinic.services._projected_clinical', return_value=None):
            with CaptureQueriesContext(connection) as source_queries:
                expected = read(self.doctor.pk, clinical=True, ref=self.ref)
        with CaptureQueriesContext(connection) as cached_queries:
            actual = read(self.doctor.pk, clinical=True, ref=self.ref)
        self.assertEqual(expected, actual)
        self.assertLess(len(cached_queries), len(source_queries))
        self.correct()
        with patch('clinic.services._projected_clinical', return_value=None):
            expected = read(self.doctor.pk, clinical=True, ref=self.ref)
        self.assertEqual(expected, read(self.doctor.pk, clinical=True, ref=self.ref))

    def test_single_read_invalid_projection_falls_back(self):
        with patch('clinic.services._projected_clinical', return_value=None):
            expected = read(self.doctor.pk, clinical=True, ref=self.ref)
        ClinicalExportProjection.objects.filter(record=self.record).update(prefix=b'broken')
        self.assertEqual(expected, read(self.doctor.pk, clinical=True, ref=self.ref))

    def test_single_read_checks_owner_before_loading_projection(self):
        for actor in (self.doctor2, self.foreign):
            with patch('clinic.services._projected_clinical') as cached:
                with self.assertRaises(DomainError):
                    read(actor.pk, clinical=True, ref=self.ref)
                cached.assert_not_called()

    def test_single_read_audit_failure_does_not_return_projection(self):
        with patch('clinic.services.write_audit', side_effect=DatabaseError('synthetic failure')):
            with self.assertRaises(DatabaseError):
                read(self.doctor.pk, clinical=True, ref=self.ref)

    def test_large_id_selection_preserves_rows_and_authorization(self):
        refs = [self.record.pk, *(uuid.uuid4() for _ in range(1999))]
        scoped = ClinicalRecord.objects.filter(encounter__store_id=self.a.pk,
                                               encounter__doctor_id=self.doctor.pk)
        expected = _record_values(scoped.filter(pk__in=refs))
        with CaptureQueriesContext(connection) as queries:
            actual = _record_values(scoped.filter(pk__in=_clinical_id_selection(refs)))
        self.assertEqual(expected, actual)
        if connection.vendor == 'mysql':
            self.assertTrue(any('JSON_TABLE' in item['sql'] for item in queries))
        with self.assertRaises(DomainError) as error:
            clinical(self.doctor.pk, {'scope': {'record_refs': [str(ref) for ref in refs]},
                                      'purpose': 'missing targets'})
        self.assertEqual('TARGET_UNAVAILABLE', error.exception.code)
        foreign = ClinicalRecord.objects.filter(encounter__doctor_id=self.foreign.pk)
        self.assertEqual([], _record_values(foreign.filter(pk__in=_clinical_id_selection(refs))))

    def test_live_projection_matches_source_without_history_queries(self):
        self.assertEqual(1, self.record.export_generation)
        self.assertTrue(ClinicalExportProjection.objects.filter(record=self.record).exists())
        with CaptureQueriesContext(connection) as queries:
            projected = self.body()
        self.assertEqual(self.body(source=True), projected)
        for query in queries:
            self.assertNotIn('clinic_clinicalrevision', query['sql'].lower())
            self.assertNotIn('clinic_medicationentry', query['sql'].lower())
            self.assertNotIn('clinic_suggestiondecision', query['sql'].lower())
        self.assertEqual(2, AuditTarget.objects.filter(target=self.record.pk,
                                                      event__action='export.clinical').count())

    def test_cached_rows_reuse_empty_link_tail_without_csv_writer_per_record(self):
        for marked in (False, True):
            Encounter.objects.filter(pk=self.ref).update(error_mark=marked)
            expected = self.body(source=True)
            with patch('clinic.exports._csv_line', side_effect=AssertionError('per-record CSV tail')):
                self.assertEqual(expected, self.body())

    def test_text_metadata_identifiers_match_normal_uuid_read(self):
        from . import exports
        def reference(queryset):
            return list(queryset.values(*exports._RECORD_FIELDS, 'export_projection__source_signature',
                'export_projection__prefix', 'export_projection__prefix_digest').order_by('encounter__created_at', 'pk'))
        other = self.create()['encounter_ref']
        Encounter.objects.filter(pk=self.ref).update(error_mark=True, rebuilt_id=other)
        with patch('clinic.exports._record_values', side_effect=reference):
            expected = self.body()
        self.assertEqual(expected, self.body())
        row = exports._record_values(ClinicalRecord.objects.filter(pk=self.record.pk))[0]
        self.assertIsInstance(row['id'], uuid.UUID)
        self.assertEqual(str(self.record.template_id), row['template_id'])
        self.assertEqual(other, row['encounter__rebuilt_id'])

    def test_correction_and_guard_refusal_keep_generation_and_full_history(self):
        key = str(uuid.uuid4())
        result = self.correct(key=key)
        self.record.refresh_from_db()
        self.assertEqual(2, self.record.export_generation)
        row = self.row()
        self.assertEqual('更正后诊断', row['diagnosis'])
        self.assertEqual([1, 2], [v['version'] for v in json.loads(row['clinical_versions'])])
        prefix = bytes(ClinicalExportProjection.objects.get(record=self.record).prefix)
        blocked = self.correct(diagnosis_value='')
        self.assertFalse(blocked['ok'])
        self.record.refresh_from_db()
        self.assertEqual(2, self.record.export_generation)
        self.assertEqual(prefix, bytes(ClinicalExportProjection.objects.get(record=self.record).prefix))
        self.assertTrue(result)

    def test_medication_correction_and_idempotency_refresh_projection(self):
        key = str(uuid.uuid4())
        values = {'drug': '合成药', 'amount': '1', 'unit': '片'}
        first = medication(self.doctor.pk, 'append', key, values, self.ref)
        self.assertEqual(first, medication(self.doctor.pk, 'append', key, values, self.ref))
        medication(self.doctor.pk, 'correct', str(uuid.uuid4()), {
            **values, 'amount': '2', 'original_entry_ref': first['saved_ref'], 'reason': '更正'}, self.ref)
        self.record.refresh_from_db()
        self.assertEqual(3, self.record.export_generation)
        chain = json.loads(self.row()['medication_chain'])
        self.assertEqual(2, len(chain))
        self.assertIn(first['saved_ref'], [entry['original_entry_ref'] for entry in chain])

    def test_adoption_and_decline_preserve_source_and_projection(self):
        def item(label):
            return Suggestion.objects.create(encounter_id=self.ref, source_version='demo', sample_key=label,
                source={'type': 'demo', 'version': 'demo', 'label': '合成演示'}, content={'diagnosis': '原建议'})
        accepted = item('accepted')
        current = ClinicalRevision.objects.get(record=self.record, version=1)
        payload = {'encounter_ref': self.ref, 'suggestion_ref': str(accepted.pk),
            'record_ref': str(self.record.pk), 'reason': '医生核对',
            'adopted_content': {'diagnosis': '采纳内容', 'original_version_ref': str(current.pk)}}
        key = str(uuid.uuid4())
        result = suggestion(self.doctor.pk, 'adopt', key, payload, str(accepted.pk))
        self.assertEqual(result, suggestion(self.doctor.pk, 'adopt', key, payload, str(accepted.pk)))
        declined = item('declined')
        prefix = bytes(ClinicalExportProjection.objects.get(record=self.record).prefix)
        suggestion(self.doctor.pk, 'decline', str(uuid.uuid4()), {
            'encounter_ref': self.ref, 'suggestion_ref': str(declined.pk), 'decision': 'declined'}, str(declined.pk))
        self.record.refresh_from_db()
        self.assertEqual(2, self.record.export_generation)
        self.assertEqual(prefix, bytes(ClinicalExportProjection.objects.get(record=self.record).prefix))
        self.assertEqual('采纳内容', self.row()['diagnosis'])
        accepted.source['label'] = '维护后来源'
        accepted.save(update_fields=['source'])
        self.assertFalse(ClinicalExportProjection.objects.filter(record=self.record).exists())
        self.assertIn('维护后来源', self.body().decode('utf-8'))

    def test_source_save_update_and_bulk_update_invalidate_atomically(self):
        for method in ('save', 'update', 'bulk_update'):
            with self.subTest(method=method):
                rebuild_projections([self.record.pk])
                revision = ClinicalRevision.objects.get(record=self.record, version=1)
                revision.diagnosis = '维护更新 ' + method
                if method == 'save':
                    revision.save(update_fields=['diagnosis'])
                elif method == 'update':
                    ClinicalRevision.objects.filter(pk=revision.pk).update(diagnosis=revision.diagnosis)
                else:
                    ClinicalRevision.objects.bulk_update([revision], ['diagnosis'])
                self.assertFalse(ClinicalExportProjection.objects.filter(record=self.record).exists())
                self.assertEqual(revision.diagnosis, self.row()['diagnosis'])

    def test_bulk_creation_deletion_and_maintenance_rollback_keep_source_authoritative(self):
        original = self.body()
        with transaction.atomic():
            ClinicalRevision.objects.filter(record=self.record).update(diagnosis='将回滚')
            self.assertFalse(ClinicalExportProjection.objects.filter(record=self.record).exists())
            transaction.set_rollback(True)
        self.assertEqual(original, self.body())
        newer = ClinicalRevision(record=self.record, version=2, diagnosis='批量新增', actor=self.doctor)
        ClinicalRevision.objects.bulk_create([newer])
        self.assertFalse(ClinicalExportProjection.objects.filter(record=self.record).exists())
        ClinicalRecord.objects.filter(pk=self.record.pk).update(current_version=2)
        self.assertEqual('批量新增', self.row()['diagnosis'])
        rebuild_projections([self.record.pk])
        newer.delete()
        self.assertFalse(ClinicalExportProjection.objects.filter(record=self.record).exists())
        ClinicalRecord.objects.filter(pk=self.record.pk).update(current_version=1)
        self.assertEqual(original, self.body())

    def test_mixed_projected_and_legacy_rows_preserve_complete_csv(self):
        other = self.record_fixture(self.create()['encounter_ref'])
        payload = {'scope': {'record_refs': [str(self.record.pk), str(other.pk)]}, 'purpose': '混合路径'}
        projected = clinical(self.doctor.pk, payload)[0]
        with patch('clinic.exports._projection_prefix', return_value=None):
            reference = clinical(self.doctor.pk, payload)[0]
        self.assertEqual(reference, projected)

    def test_cached_export_still_rolls_back_when_audit_fails(self):
        before = ExportRequest.objects.count()
        with patch('clinic.exports.write_audit', side_effect=DatabaseError('synthetic audit outage')):
            with self.assertRaises(DatabaseError):
                self.body()
        self.assertEqual(before, ExportRequest.objects.count())

    def test_lock_note_cannot_survive_its_source_transaction(self):
        from .models import _lock_projection_records
        from .export_projection import note_encounter_lock
        with transaction.atomic():
            note_encounter_lock(self.record, Encounter.objects.get(pk=self.ref))
        revision = ClinicalRevision.objects.get(record=self.record, version=1)
        revision.record = self.record
        with patch('clinic.models._lock_projection_records', wraps=_lock_projection_records) as lock:
            revision.reason = '独立维护事务'
            revision.save(update_fields=['reason'])
        self.assertEqual(1, lock.call_count)

    def test_missing_stale_and_corrupt_projection_fall_back_to_source(self):
        reference = self.body(source=True)
        for values in ({'prefix': b'corrupt'}, {'source_signature': '0' * 64}):
            rebuild_projections([self.record.pk])
            ClinicalExportProjection.objects.filter(record=self.record).update(**values)
            self.assertEqual(reference, self.body())
        ClinicalExportProjection.objects.filter(record=self.record).delete()
        self.assertEqual(reference, self.body())

    def test_error_mark_rebuild_and_permission_checks_stay_live(self):
        other = self.create()['encounter_ref']
        Encounter.objects.filter(pk=self.ref).update(error_mark=True, rebuilt_id=other)
        row = self.row()
        self.assertEqual('true', row['error_mark'])
        self.assertEqual([other], json.loads(row['related_encounter_refs']))
        with self.assertRaises(DomainError) as caught:
            clinical(self.foreign.pk, {'scope': {'record_refs': [str(self.record.pk)]}, 'purpose': '拒绝'})
        self.assertEqual('TARGET_UNAVAILABLE', caught.exception.code)
        Review.objects.filter(candidate=self.template).update(decision='reject')
        with self.assertRaises(DomainError):
            self.body()

    def test_projection_failure_rolls_back_source_and_generation(self):
        before = ClinicalExportProjection.objects.get(record=self.record)
        key = str(uuid.uuid4())
        with patch('clinic.export_projection.ClinicalExportProjection.objects.bulk_create',
                   side_effect=DatabaseError('synthetic projection failure')):
            with self.assertRaises(DatabaseError):
                self.correct(key=key)
        self.record.refresh_from_db()
        self.assertEqual((1, 1), (self.record.current_version, self.record.export_generation))
        self.assertEqual(1, ClinicalRevision.objects.filter(record=self.record).count())
        self.assertEqual(before.prefix_digest, ClinicalExportProjection.objects.get(record=self.record).prefix_digest)
        self.assertFalse(Operation.objects.filter(key=key).exists())

    def test_backfill_builds_legacy_records_without_advancing_source(self):
        other = self.record_fixture(self.create()['encounter_ref'])
        call_command('rebuild_export_projections', batch_size=1, stdout=io.StringIO())
        self.assertEqual(2, ClinicalExportProjection.objects.count())
        other.refresh_from_db()
        self.assertEqual(0, other.export_generation)
        self.assertEqual(self.body(source=True), self.body())

    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_medication_writes_preserve_both_entries(self):
        def write(number):
            close_old_connections()
            try:
                return medication(self.doctor.pk, 'append', str(uuid.uuid4()), {
                    'drug': '合成药', 'amount': str(number), 'unit': '片'}, self.ref)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            saved = list(pool.map(write, (1, 2)))
        self.record.refresh_from_db()
        self.assertEqual(3, self.record.export_generation)
        self.assertEqual({item['saved_ref'] for item in saved},
                         {item['entry_ref'] for item in json.loads(self.row()['medication_chain'])})

    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_backfill_and_source_maintenance_never_publish_stale_prefix(self):
        def work(mode):
            close_old_connections()
            try:
                for n in range(4):
                    if mode == 'source':
                        ClinicalRevision.objects.filter(record=self.record, version=1).update(diagnosis=f'维护{n}')
                    else:
                        rebuild_projections([self.record.pk])
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(work, ('source', 'projection')))
        self.assertEqual('维护3', self.row()['diagnosis'])
