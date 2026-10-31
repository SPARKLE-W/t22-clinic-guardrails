from django.db import connection
from django.test import TestCase
from governance.models import Operation
from .models import AuditTarget, ClinicalRevision, ReceptionRevision


class CoveredForeignKeyIndexTests(TestCase):
    def test_hot_foreign_keys_rely_on_existing_composite_indexes(self):
        cases = [
            (AuditTarget, 'event_id'),
            (ClinicalRevision, 'record_id'),
            (Operation, 'actor_id'),
            (ReceptionRevision, 'encounter_id'),
        ]
        with connection.cursor() as cursor:
            for model, column in cases:
                with self.subTest(model=model.__name__):
                    constraints = connection.introspection.get_constraints(cursor, model._meta.db_table)
                    self.assertTrue(any(item['unique'] and item['columns'] and item['columns'][0] == column
                                        for item in constraints.values()))
                    self.assertFalse(any(item['index'] and item['columns'] == [column]
                                         for item in constraints.values()))
