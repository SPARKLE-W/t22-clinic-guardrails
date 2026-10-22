"""PRE large CSV/snapshot tests; run only via Django's isolated MySQL test DB.

No service implementation is changed here. Fixture rows and historical text are
synthetic and bulk-created; they are not evidence of real clinical decisions.
"""

import csv
import io
import json
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.db import OperationalError, connections
from django.test import TransactionTestCase, tag

from .models import (
    Audit, AuditTarget, Encounter, Medication, Operation, PolicyHead,
    Profile, Record, Revision, Store,
)
from .services import export_medical, write_medical


@tag("export_scope", "mysql_only")
class LargeExportScopeTests(TransactionTestCase):
    """Catch CSV truncation/duplication, history loss and missing parent locks."""

    scope_size = 4000
    store_count = 5
    original_text = 'PRE 原始诊断 O\'Reilly, "引号"\n保留旧版'
    corrected_text = 'PRE 首次更正, "中文"\n不是临床建议'
    concurrent_text = 'PRE 导出后更正, "并发"\n必须属于下一快照'
    original_reason = 'PRE 初次更正理由, "保留"\n合成记录'
    concurrent_reason = 'PRE 并发更正理由, "真实测试调用"\n非真人诊疗'

    def setUp(self):
        db = connections["default"]
        self.assertEqual("mysql", db.vendor, "This experiment must use real MySQL")
        self.assertEqual("t22_probe_test", db.settings_dict["NAME"],
                         "Do not run these bulk fixtures in the main probe DB")
        with db.cursor() as cursor:
            cursor.execute("SELECT @@transaction_isolation")
            self.assertEqual("READ-COMMITTED", cursor.fetchone()[0])
        self.prefix = "xs_" + uuid.uuid4().hex[:12]
        self.export_measurements = []
        started = time.perf_counter()
        # One password hash per whole test, never one PBKDF2 per record.
        password_hash = make_password("PRE-export-synthetic-not-a-login-secret")
        Store.objects.bulk_create([
            Store(code=f"{self.prefix}_store_{index}") for index in range(self.store_count)
        ])
        self.stores = list(Store.objects.filter(code__startswith=self.prefix).order_by("code"))
        users = []
        for index in range(self.store_count):
            for role in ("doctor", "frontdesk", "manager"):
                users.append(User(username=f"{self.prefix}_{index}_{role}",
                                  password=password_hash, is_active=True))
        users.append(User(username=f"{self.prefix}_other_doctor", password=password_hash,
                          is_active=True))
        User.objects.bulk_create(users)
        # MySQL bulk_create need not populate auto IDs; re-read by unique name.
        by_name = {user.username: user for user in
                   User.objects.filter(username__startswith=self.prefix)}
        profiles, self.doctors, self.frontdesks = [], [], []
        for index, store in enumerate(self.stores):
            for role in ("doctor", "frontdesk", "manager"):
                user = by_name[f"{self.prefix}_{index}_{role}"]
                profiles.append(Profile(user=user, store=store, role=role))
                if role == "doctor":
                    self.doctors.append(user)
                elif role == "frontdesk":
                    self.frontdesks.append(user)
        self.other_doctor = by_name[f"{self.prefix}_other_doctor"]
        profiles.append(Profile(user=self.other_doctor, store=self.stores[0], role="doctor"))
        Profile.objects.bulk_create(profiles)
        PolicyHead.objects.bulk_create([PolicyHead(store=store) for store in self.stores])
        self.doctor = self.doctors[0]
        self.owned_records, self.encounter_by_record, self.template_by_record = [], {}, {}
        self.initial_diagnosis_by_record = {}
        encounters, records, revisions = [], [], []
        self.foreign_record_id = None
        for store_index, store in enumerate(self.stores):
            for index in range(self.scope_size):
                encounter = Encounter(
                    store=store, doctor=self.doctors[store_index],
                    receptionist=self.frontdesks[store_index],
                    pet_id=f"{self.prefix}_pet_{store_index}_{index % 1200}",
                    complaint="PRE synthetic reception, no owner contact",
                )
                record = Record(
                    encounter=encounter, diagnosis=f"PRE diagnosis {store_index}:{index}",
                    template_version=f"PRE-template-{1 + index % 2}",
                )
                encounters.append(encounter)
                records.append(record)
                revisions.append(Revision(
                    record=record, version=1, actor=self.doctors[store_index],
                    content={"diagnosis": record.diagnosis, "examination": "", "adopted_text": ""},
                ))
                if store_index == 0:
                    self.owned_records.append(record)
                    self.encounter_by_record[str(record.id)] = str(encounter.id)
                    self.template_by_record[str(record.id)] = record.template_version
                    self.initial_diagnosis_by_record[str(record.id)] = record.diagnosis
                elif self.foreign_record_id is None:
                    self.foreign_record_id = record.id
        Encounter.objects.bulk_create(encounters, batch_size=1000)
        Record.objects.bulk_create(records, batch_size=1000)
        Revision.objects.bulk_create(revisions, batch_size=1000)
        self.target = self.owned_records[0]
        Revision.objects.filter(record=self.target, version=1).update(content={
            "diagnosis": self.original_text, "examination": "", "adopted_text": "",
        })
        Record.objects.filter(pk=self.target.id).update(version=2, diagnosis=self.corrected_text)
        Revision.objects.create(
            record=self.target, version=2, actor=self.doctor, reason=self.original_reason,
            content={"diagnosis": self.corrected_text, "examination": "", "adopted_text": ""},
        )
        self.original_med = Medication.objects.create(
            record=self.target, drug='PRE drug, "A"', amount="1", unit="合成单位", actor=self.doctor,
        )
        self.med_reason = 'PRE 追加更正, "理由"\n原项不变'
        self.corrected_med = Medication.objects.create(
            record=self.target, original=self.original_med, drug='PRE drug, "A"',
            amount="2", unit="合成单位", actor=self.doctor, reason=self.med_reason,
        )
        # Same pet ID in another doctor's visit must not confer access.
        self.other_encounter = Encounter.objects.create(
            store=self.stores[0], doctor=self.other_doctor, receptionist=self.frontdesks[0],
            pet_id=encounters[0].pet_id,
        )
        self.other_record = Record.objects.create(encounter=self.other_encounter,
                                                   diagnosis="PRE same-store other doctor")
        self.ids = [str(record.id) for record in reversed(self.owned_records)]
        self.expected_ids = set(self.ids)
        self.purpose = 'PRE 全量导出, "用途"\n只含当前医生授权集合'
        owned_from_db = set(map(str, Record.objects.filter(
            encounter__store=self.stores[0], encounter__doctor=self.doctor,
        ).values_list("id", flat=True)))
        self.assertEqual(self.expected_ids, owned_from_db)
        self.assertEqual(4000, len(owned_from_db))
        self.emit("fixture", stores=5, primary_records=20000, negative_scope_records=1,
                  owned_records=4000, preparation_ms=round((time.perf_counter()-started)*1000, 3))

    def emit(self, phase, **values):
        print("EXPORT_SCOPE " + json.dumps({"test": self._testMethodName, "phase": phase,
                                              **values}, ensure_ascii=False), flush=True)

    def successful_export_ids(self):
        return set(Audit.objects.filter(actor=self.doctor, action="export.clinical",
                                        result="success").values_list("id", flat=True))

    def measure_export(self, *, before_expose=None):
        queries = 0

        def observe(execute, sql, params, many, context):
            nonlocal queries
            queries += 1
            return execute(sql, params, many, context)

        started = time.perf_counter()
        with connections["default"].execute_wrapper(observe):
            response = export_medical(self.doctor, self.ids, self.purpose,
                                      before_expose=before_expose)
        measurement = {"status": response["status"], "sql_statements": queries,
                       "elapsed_ms": round((time.perf_counter()-started)*1000, 3),
                       "declared_records": response.get("record_count"),
                       "utf8_bytes": response.get("utf8_bytes")}
        self.export_measurements.append(measurement)
        self.emit("export_return", **measurement)
        return response

    def assert_csv_and_audit(self, response, before_audits, *, target_version=2):
        self.assertEqual(200, response["status"], response)
        self.assertEqual(4000, response["record_count"])
        self.assertEqual(len(response["csv"].encode("utf-8")), response["utf8_bytes"])
        rows = list(csv.DictReader(io.StringIO(response["csv"], newline="")))
        current = [row for row in rows if row["kind"] == "record"]
        revisions = [row for row in rows if row["kind"] == "revision"]
        medications = [row for row in rows if row["kind"] == "medication"]
        self.assertEqual(4000, len(current))
        self.assertEqual(self.expected_ids, set(row["record_id"] for row in current))
        self.assertTrue(all(count == 1 for count in Counter(row["record_id"] for row in current).values()))
        self.assertEqual(Counter({"record": 4000, "revision": 3999 + target_version,
                                  "medication": 2}), Counter(row["kind"] for row in rows))
        for row in rows:
            self.assertIn(row["record_id"], self.expected_ids)
            self.assertEqual(self.encounter_by_record[row["record_id"]], row["encounter_id"])
            self.assertEqual(self.template_by_record[row["record_id"]], row["template_version"])
        revision_keys = [(row["record_id"], int(row["version"])) for row in revisions]
        expected_revision_keys = {(record_id, 1) for record_id in self.expected_ids}
        expected_revision_keys.update((str(self.target.id), version)
                                      for version in range(2, target_version + 1))
        self.assertEqual(expected_revision_keys, set(revision_keys))
        self.assertEqual(len(expected_revision_keys), len(revision_keys))
        for row in current:
            self.assertEqual(target_version if row["record_id"] == str(self.target.id) else 1,
                             int(row["version"]))
            content = json.loads(row["payload"])
            expected = self.initial_diagnosis_by_record[row["record_id"]]
            if row["record_id"] == str(self.target.id):
                expected = self.corrected_text if target_version == 2 else self.concurrent_text
            self.assertEqual(expected, content["diagnosis"])
            self.assertEqual("", content["examination"])
            self.assertEqual("", content["adopted_text"])
        target_current = next(row for row in current if row["record_id"] == str(self.target.id))
        expected_diagnosis = self.corrected_text if target_version == 2 else self.concurrent_text
        self.assertEqual(expected_diagnosis, json.loads(target_current["payload"])["diagnosis"])
        target_history = {int(row["version"]): row for row in revisions
                          if row["record_id"] == str(self.target.id)}
        self.assertEqual(self.original_text, json.loads(target_history[1]["payload"])["diagnosis"])
        self.assertEqual(self.corrected_text, json.loads(target_history[2]["payload"])["diagnosis"])
        self.assertEqual(self.original_reason, target_history[2]["reason"])
        if target_version == 3:
            self.assertEqual(self.concurrent_text, json.loads(target_history[3]["payload"])["diagnosis"])
            self.assertEqual(self.concurrent_reason, target_history[3]["reason"])
        for row in revisions:
            self.assertEqual(str(self.doctor.id), row["actor_id"])
            self.assertTrue(row["created_at"])
            expected = self.initial_diagnosis_by_record[row["record_id"]]
            if row["record_id"] == str(self.target.id):
                expected = {1: self.original_text, 2: self.corrected_text,
                            3: self.concurrent_text}[int(row["version"])]
            self.assertEqual(expected, json.loads(row["payload"])["diagnosis"])
        by_medication_id = {json.loads(row["payload"])["id"]: row for row in medications}
        self.assertEqual({str(self.original_med.id), str(self.corrected_med.id)}, set(by_medication_id))
        self.assertEqual(2, len(medications))
        original, correction = by_medication_id[str(self.original_med.id)], by_medication_id[str(self.corrected_med.id)]
        self.assertEqual("", original["original_id"])
        self.assertEqual(str(self.original_med.id), correction["original_id"])
        self.assertEqual("1", json.loads(original["payload"])["amount"])
        self.assertEqual("2", json.loads(correction["payload"])["amount"])
        self.assertEqual(self.med_reason, correction["reason"])
        self.assertTrue(all(row["record_id"] == str(self.target.id) for row in medications))
        self.original_med.refresh_from_db()
        self.assertEqual("1", self.original_med.amount)
        new_audits = self.successful_export_ids() - before_audits
        self.assertEqual(1, len(new_audits))
        event = Audit.objects.get(id=new_audits.pop())
        audit_targets = list(map(str, AuditTarget.objects.filter(audit=event).values_list("record_id", flat=True)))
        self.assertEqual(4000, len(audit_targets))
        self.assertEqual(self.expected_ids, set(audit_targets))
        self.assertEqual(4000, len(set(audit_targets)))
        self.assertEqual(self.expected_ids, set(event.private_details["scope"]))
        self.assertEqual(4000, len(event.private_details["scope"]))
        self.assertEqual(self.purpose, event.private_details["purpose"])
        self.assertEqual(self.doctor.id, event.actor_id)
        self.assertEqual(self.stores[0].id, event.event_store_id)
        self.emit("verified", logical_records=4000, csv_rows=len(rows),
                  revision_rows=len(revisions), medication_rows=2, audit_targets=len(audit_targets),
                  target_version=target_version)
        return event.id

    @tag("export_scope_query_budget")
    def test_4000_export_uses_bounded_queries_without_truncating_scope(self):
        # A per-record Revision/Medication query regresses to ~8000 SQL calls.
        # <60 is an experiment guard, not a new SRS latency/query requirement.
        before = self.successful_export_ids()
        response = self.measure_export()
        measurement = self.export_measurements[-1]
        self.assert_csv_and_audit(response, before)
        self.emit("query_budget", maximum_exclusive=60,
                  observed=measurement["sql_statements"],
                  complete_utf8_bytes=len(response["csv"].encode("utf-8")))
        self.assertLess(measurement["sql_statements"], 60,
                        "PRE query budget: batch-load history/medication; keep all 4000 records and audit")

    @tag("export_scope_full")
    def test_4000_record_full_scope_history_and_audit_objects(self):
        # Would catch a page-sized cap, duplicate join rows, lost correction
        # linkage, a template swap, or an audit recording only part of the set.
        before = self.successful_export_ids()
        response = self.measure_export()
        self.assert_csv_and_audit(response, before)
        success_ids = self.successful_export_ids()
        for forbidden_id in (self.foreign_record_id, self.other_record.id):
            with self.subTest(forbidden_kind="cross_store" if forbidden_id == self.foreign_record_id
                              else "same_store_other_doctor_same_pet"):
                denied = export_medical(self.doctor, self.ids + [str(forbidden_id)], self.purpose)
                self.assertEqual(403, denied["status"])
                self.assertNotIn("csv", denied)
                self.assertEqual(success_ids, self.successful_export_ids())

    @tag("export_scope_concurrency")
    def test_4000_export_shared_locks_block_correction_and_keep_one_snapshot(self):
        # Without the export's shared Encounter locks the writer completes;
        # without atomic revision reads, pre/post snapshots or audit sets differ.
        export_holds_locks = threading.Event()
        release_export = threading.Event()
        writer_lock_request = threading.Event()
        errors = []
        operation_key = "xs-correct-" + uuid.uuid4().hex
        before = self.successful_export_ids()

        def hold_export():
            export_holds_locks.set()
            if not release_export.wait(timeout=25):
                raise TimeoutError("test coordinator did not release export")

        def exporting():
            connections.close_all()
            try:
                return self.measure_export(before_expose=hold_export)
            finally:
                connections.close_all()

        def observe_writer(execute, sql, params, many, context):
            is_parent_lock = "probe_encounter" in sql.lower() and "for update" in sql.lower()
            if is_parent_lock:
                writer_lock_request.set()
            try:
                return execute(sql, params, many, context)
            except OperationalError as exc:
                errors.append({"mysql_errno": exc.args[0], "encounter_for_update": is_parent_lock})
                raise

        data = {"expected_version": 2, "diagnosis": self.concurrent_text,
                "reason": self.concurrent_reason, "correction": True}

        def correcting():
            connections.close_all()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET SESSION innodb_lock_wait_timeout=1")
                with connections["default"].execute_wrapper(observe_writer):
                    return write_medical(self.doctor, self.target.encounter_id, "diagnosis",
                                         operation_key, data)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            export_future = pool.submit(exporting)
            try:
                self.assertTrue(export_holds_locks.wait(timeout=60), "export never reached its locked snapshot")
                writer_future = pool.submit(correcting)
                waiting_result = writer_future.result(timeout=12)
                self.assertTrue(writer_lock_request.is_set())
                self.assertEqual([{"mysql_errno": 1205, "encounter_for_update": True}], errors)
                self.assertEqual({"status": 503, "code": "PERSISTENCE_UNCONFIRMED"}, waiting_result)
                saved = Record.objects.get(pk=self.target.id)
                self.assertEqual(2, saved.version)
                self.assertEqual(self.corrected_text, saved.diagnosis)
                self.assertFalse(Operation.objects.filter(actor=self.doctor, operation_key=operation_key).exists())
                self.assertEqual(before, self.successful_export_ids(), "export audit must not commit before release")
                self.emit("real_lock_wait", mysql_errno=1205, encounter_for_update=True,
                          database_writer_status=waiting_result["status"], target_version_while_held=2)
            finally:
                release_export.set()
            response = export_future.result(timeout=60)
        first_audit_id = self.assert_csv_and_audit(response, before, target_version=2)
        # Same failed operation may now retry after rollback/release. It must
        # become a new committed version, never change the already emitted CSV.
        corrected = write_medical(self.doctor, self.target.encounter_id, "diagnosis", operation_key, data)
        self.assertEqual(200, corrected["status"], corrected)
        self.assertEqual(3, corrected["version"])
        self.assertEqual(1, Operation.objects.filter(actor=self.doctor, operation_key=operation_key).count())
        after_first = self.successful_export_ids()
        post = self.measure_export()
        second_audit_id = self.assert_csv_and_audit(post, after_first, target_version=3)
        self.assertNotEqual(first_audit_id, second_audit_id)
        self.assertEqual(2, len(self.successful_export_ids() - before))
        self.assertEqual(4000, AuditTarget.objects.filter(audit_id=first_audit_id).count())
        self.assertNotEqual(response["csv"], post["csv"])
