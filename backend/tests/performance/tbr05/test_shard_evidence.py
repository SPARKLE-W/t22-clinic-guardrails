import importlib.util
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('shard_evidence', Path(__file__).with_name('shard_evidence.py'))
evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence)


class ShardEvidenceTests(unittest.TestCase):
    def test_reports_require_same_clock_source_fixture_and_complete_window(self):
        with tempfile.TemporaryDirectory() as folder:
            clock_path = Path(folder) / 'clock.json'
            clock = {'shard_count': 4, 'concurrency': 360, 'warmup_seconds': 60,
                     'formal_seconds': 120, 'monotonic_start': 1000.125}
            clock_path.write_text(json.dumps(clock))
            digest = hashlib.sha256(clock_path.read_bytes()).hexdigest()
            prefixes = []
            for index in range(4):
                prefix = Path(folder) / f'part{index}'
                prefixes.append(prefix)
                report = {'shard_index': index, 'shard_count': 4, 'concurrency': 90,
                          'worker_offset': index * 90, 'request_id_base': index * 10 ** 12,
                          'common_clock_sha256': digest, 'common_start_monotonic': 1000.125,
                          'warmup_seconds': 60, 'formal_seconds': 120, 'actual_duration_seconds': 181,
                          'fixture_database': 'synthetic', 'load_script_sha256': 'same-source',
                          'gil_switch_interval_ms': .1, 'target_endpoint_count': 16}
                prefix.with_suffix('.summary.json').write_text(json.dumps(report))
            self.assertEqual(4, len(evidence.verify_shard_reports(prefixes, clock_path)[1]))
            path = prefixes[-1].with_suffix('.summary.json')
            original = json.loads(path.read_text())
            for change in ({'common_clock_sha256': 'wrong'}, {'fixture_database': 'other'},
                           {'load_script_sha256': 'other'}, {'actual_duration_seconds': 179}):
                path.write_text(json.dumps({**original, **change}))
                with self.subTest(change=change), self.assertRaises(ValueError):
                    evidence.verify_shard_reports(prefixes, clock_path)

    def test_incomplete_sets_and_nonfinite_sample_times_fail_closed(self):
        with self.assertRaises(ValueError):
            evidence.merge_request_rows([], [])
        with self.assertRaises(ValueError):
            evidence.merge_request_rows([Path('unused')], [])
        with self.assertRaises(ValueError):
            evidence.merge_inflight_samples([], 90, 1, 2)
        for at in (float('nan'), float('inf'), -1):
            with self.subTest(at=at), self.assertRaises(ValueError):
                evidence.merge_inflight_samples([[{'seconds': at, 'in_flight': 1, 'validation_pending': 0}]], 90, 1, 2)

    def test_samples_sum_actual_counts_and_preserve_component_times(self):
        parts = [[{'seconds': n / 10, 'in_flight': i + 1, 'validation_pending': 0}
                  for n in range(31)] for i in range(4)]
        merged = evidence.merge_inflight_samples(parts, 90, 1, 2)
        self.assertTrue(all(r['in_flight'] == 10 for r in merged))
        self.assertEqual([0, 0, 0, 0], merged[0]['shard_sample_seconds'])

    def test_other_shards_cannot_hide_one_shards_missing_samples(self):
        good = [{'seconds': n / 10, 'in_flight': 90, 'validation_pending': 0} for n in range(31)]
        bad = [r for r in good if not 1.2 < r['seconds'] < 2.2]
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            evidence.merge_inflight_samples([good, bad, good, good], 90, 1, 2)

    def test_bad_counts_are_rejected_instead_of_clamped(self):
        samples = [{'seconds': n / 10, 'in_flight': 91, 'validation_pending': 0} for n in range(31)]
        with self.assertRaises(ValueError):
            evidence.merge_inflight_samples([samples], 90, 1, 2)

    def test_request_merge_preserves_original_intervals_and_rejects_foreign_workers(self):
        with tempfile.TemporaryDirectory() as folder:
            prefixes, reports = [], []
            for index in range(4):
                prefix = Path(folder) / f'run-shard-{index}'
                prefixes.append(prefix)
                reports.append({'worker_offset': index, 'concurrency': 1, 'request_id_base': index * 10 ** 12})
                row = {'worker_id': index, 'request_id': index * 10 ** 12,
                       'started': .123456789, 'ended': .234567891}
                prefix.with_suffix('.requests.jsonl').write_text(json.dumps(row) + '\n')
            merged = evidence.merge_request_rows(prefixes, reports)
            self.assertEqual([.123456789] * 4, [r['started'] for r in merged])
            self.assertEqual([.234567891] * 4, [r['ended'] for r in merged])
            bad = {'worker_id': 0, 'request_id': 10 ** 12, 'started': .1, 'ended': .2}
            prefixes[1].with_suffix('.requests.jsonl').write_text(json.dumps(bad))
            with self.assertRaisesRegex(ValueError, 'worker'):
                evidence.merge_request_rows(prefixes, reports)


if __name__ == '__main__':
    unittest.main()
