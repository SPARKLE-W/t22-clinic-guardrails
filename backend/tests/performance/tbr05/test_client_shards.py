import hashlib
import importlib.util
import json
import random
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).with_name('03_tbr05_load.py')
SPEC = importlib.util.spec_from_file_location('shard_driver_tests', SCRIPT)
load = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {'requests': types.ModuleType('requests'), 'psutil': types.ModuleType('psutil')}):
    SPEC.loader.exec_module(load)


class ClientPartitionTests(unittest.TestCase):
    def test_target_partitions_cover_fixed_pools_without_overlap_and_keep_ownership_on_wrap(self):
        for total in (load.GUARD_TARGETS, load.DIAGNOSIS_TARGETS):
            seen = set()
            for index in range(4):
                size = load.partition_size(total, index, 4)
                owned = {load.partition_target(n, total, index, 4) for n in range(size)}
                self.assertFalse(seen & owned)
                self.assertTrue(all(target % 4 == index for target in owned))
                self.assertEqual(index, load.partition_target(size, total, index, 4))
                seen.update(owned)
            self.assertEqual(set(range(total)), seen)
        self.assertEqual(2, load.partition_target(9, 7, 0, 1))

    def test_adoption_slots_stay_with_record_owner_and_exhaust_independently(self):
        with patch.object(load, 'ADOPTION_RECORDS', 7), patch.object(load, 'ADOPTION_TARGETS', 112):
            all_pairs = set()
            for index in range(4):
                size = load.partition_size(7, index, 4)
                pairs = {load.adoption_target(n, index, 4) for n in range(size * 16)}
                self.assertTrue(all(record % 4 == index for slot, record in pairs))
                self.assertFalse(all_pairs & pairs)
                all_pairs.update(pairs)
                with self.assertRaisesRegex(RuntimeError, 'pool exhausted'):
                    load.adoption_target(size * 16, index, 4)
            self.assertEqual({(slot, record) for slot in range(16) for record in range(7)}, all_pairs)

    def test_request_ids_are_unique_and_each_shard_preserves_mix_and_write_subtype_phase(self):
        ids = set()
        with patch.object(load, 'CLIENT_SHARD_COUNT', 1, create=True), \
             patch.object(load, 'CLIENT_SHARD_INDEX', 0, create=True), patch.object(load, 'SERIAL', load.itertools.count()):
            for index in range(4):
                base = load.configure_client_shard(4, index)
                self.assertEqual(0, base % 20)
                rows = [load.next_task() for _ in range(40)]
                self.assertFalse(ids & {n for n, kind in rows})
                ids.update(n for n, kind in rows)
                self.assertEqual([load.MIX[n % 20] for n in range(40)], [kind for n, kind in rows])
                self.assertEqual([n % 5 for n in range(40)], [n % 5 for n, kind in rows])
        self.assertEqual(160, len(ids))

    def test_payloads_use_owned_records_and_reuse_record_only_with_next_adoption_slot(self):
        for index in range(4):
            with patch.object(load, 'CLIENT_SHARD_COUNT', 4, create=True), \
                 patch.object(load, 'CLIENT_SHARD_INDEX', index, create=True), \
                 patch.object(load, 'next_target', return_value=0):
                guard = load.select_request('guard', 0, random.Random(2))
                diagnosis = load.select_request('write', 0, random.Random(2))
                adoption = load.select_request('write', 14, random.Random(2))
                self.assertIn(load.ref('encounter', index, 0), guard[2])
                self.assertEqual(load.ref('record', index, 1), diagnosis[3]['original_content_ref'])
                self.assertEqual(load.ref('record', index, 2), adoption[3]['record_ref'])
                capacity = load.partition_size(load.ADOPTION_RECORDS, index, 4)
                with patch.object(load, 'next_target', return_value=capacity):
                    second = load.select_request('write', 14, random.Random(2))
                self.assertEqual(adoption[3]['record_ref'], second[3]['record_ref'])
                self.assertNotEqual(adoption[3]['suggestion_ref'], second[3]['suggestion_ref'])

    def test_worker_endpoint_assignments_add_to_original_360_worker_distribution(self):
        merged = [0] * 16
        for index in range(4):
            local = load.target_worker_assignments(90, 16, worker_offset=index * 90)
            merged = [a + b for a, b in zip(merged, local)]
        self.assertEqual(load.target_worker_assignments(360, 16), merged)


class CommonClockTests(unittest.TestCase):
    def test_clock_preserves_monotonic_origin_and_checks_run_parameters(self):
        clock = {'clock_version': 1, 'shard_count': 4, 'concurrency': 360,
                 'warmup_seconds': 60, 'formal_seconds': 120,
                 'monotonic_start': 1000.125, 'utc_start': '2026-11-06T00:00:00+00:00'}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'clock.json'
            path.write_text(json.dumps(clock))
            parsed = load.read_common_clock(path, 4, 90, 60, 120)
            self.assertEqual(1000.125, parsed['monotonic_start'])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), parsed['sha256'])
            with self.assertRaises(ValueError):
                load.read_common_clock(path, 4, 80, 60, 120)
            for change in ({'monotonic_start': float('nan')}, {'utc_start': '2026-11-06T00:00:00'}):
                path.write_text(json.dumps({**clock, **change}))
                with self.assertRaises(ValueError):
                    load.read_common_clock(path, 4, 90, 60, 120)


if __name__ == '__main__':
    unittest.main()
