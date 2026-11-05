from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from governance.models import Account
from .exports import guard, SUMMARY
import importlib
from .models import GuardHit
from .tests import ClinicFixture
from .writes import diagnosis
import uuid


class GuardHitStoreTests(ClinicFixture):
    def setUp(self):
        super().setUp()
        self.ref = self.create()['encounter_ref']
        diagnosis(self.doctor.pk, 'record', str(uuid.uuid4()), {}, self.ref)
        self.hit = GuardHit.objects.get()

    def clone(self, **extra):
        return GuardHit(evaluation=self.hit.evaluation, rule_ref='COPY',
                        rule_version='1', field='diagnosis', **extra)

    def test_service_and_bulk_creation_derive_store_from_evaluation(self):
        self.assertEqual(self.a.pk, self.hit.store_id)
        rows = [self.clone(), self.clone()]
        GuardHit.objects.bulk_create(rows)
        self.assertEqual([self.a.pk] * 2, [row.store_id for row in rows])
        self.assertEqual(3, GuardHit.objects.filter(store=self.a).count())

    def test_wrong_store_is_rejected_without_partial_batch(self):
        with self.assertRaises(ValidationError):
            GuardHit.objects.bulk_create([self.clone(), self.clone(store=self.b)])
        self.assertEqual(1, GuardHit.objects.count())
        with self.assertRaises(ValidationError):
            self.clone(store=self.b).save()
        self.assertEqual(1, GuardHit.objects.count())

    def test_bulk_creation_rolls_back_with_source_transaction(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                GuardHit.objects.bulk_create([self.clone()])
                raise RuntimeError('rollback')
        self.assertEqual(1, GuardHit.objects.filter(store=self.a).count())

    def test_bulk_lineage_changes_require_source_aware_save(self):
        for change in ({'store': self.b}, {'evaluation_id': self.hit.evaluation_id}):
            with self.assertRaises(ValidationError):
                GuardHit.objects.filter(pk=self.hit.pk).update(**change)
        self.hit.rule_ref = 'UPDATED'
        GuardHit.objects.bulk_update([self.hit], ['rule_ref'])
        self.hit.refresh_from_db()
        self.assertEqual(self.a.pk, self.hit.store_id)
        self.assertEqual('UPDATED', self.hit.rule_ref)

    def test_original_store_chain_rejects_corrupt_key_and_backfill_repairs_it(self):
        field = GuardHit._meta.get_field('store').target_field
        key = GuardHit._meta.pk
        with connection.cursor() as cursor:
            cursor.execute('UPDATE clinic_guardhit SET store_id=%s WHERE id=%s',
                           [field.get_db_prep_value(self.b.pk, connection),
                            key.get_db_prep_value(self.hit.pk, connection)])
        foreign_manager = Account.objects.create_user(username='foreign-manager', store=self.b, role='manager')
        _, _, count = guard(foreign_manager.pk, SUMMARY, {})
        self.assertEqual(0, count)
        apps = MigrationExecutor(connection).loader.project_state([('clinic', '0012_guard_hit_store')]).apps
        migration = importlib.import_module('clinic.migrations.0012_guard_hit_store')
        migration.backfill_stores(apps, connection.schema_editor())
        self.hit.refresh_from_db()
        self.assertEqual(self.a.pk, self.hit.store_id)
        _, _, count = guard(self.manager.pk, SUMMARY, {})
        self.assertEqual(1, count)

    def test_positional_conflict_updates_cannot_bypass_lineage_validation(self):
        with self.assertRaises(ValidationError):
            GuardHit.objects.bulk_create([self.clone()], 500, False, True, ['store'], ['id'])
        self.assertEqual(1, GuardHit.objects.count())
