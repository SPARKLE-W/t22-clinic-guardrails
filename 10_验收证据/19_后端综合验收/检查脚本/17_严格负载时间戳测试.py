import importlib.util
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).with_name('07_正式HTTP负载.py')
SPEC = importlib.util.spec_from_file_location('strict_load', SCRIPT)
strict_load = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(strict_load)


class AbsoluteTimestampTests(unittest.TestCase):
    def test_perf_counter_offset_maps_elapsed_to_utc(self):
        origin = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.assertEqual('2025-01-02T03:10:06.000Z', strict_load.utc_at(origin, 361))

    def test_request_timestamps_use_perf_counter_offsets(self):
        origin = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        row = {'started': 861.0, 'first': 861.025, 'ended': 861.3}

        self.assertEqual({
            'started_at_utc': '2025-01-02T03:18:26.000Z',
            'first_byte_at_utc': '2025-01-02T03:18:26.025Z',
            'ended_at_utc': '2025-01-02T03:18:26.300Z',
        }, strict_load.request_timestamps(row, 0.0, origin))

    def test_request_without_first_byte_keeps_null_timestamp(self):
        origin = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        row = {'started': 300.0, 'first': None, 'ended': 301.0}

        self.assertIsNone(strict_load.request_timestamps(row, 0.0, origin)['first_byte_at_utc'])


class ServerProfileHeaderTests(unittest.TestCase):
    def test_parses_all_stage_and_identity_headers(self):
        headers = {
            'Server-Timing': 'app;dur=18.2500, sql;dur=6.5000, commit;dur=1.2500, queue;dur=0.7500',
            'X-T22-SQL-Count': '9',
            'X-T22-Server-PID': '4120',
            'X-T22-Process-CPU': '18.75',
            'X-T22-App-Start': '123.0',
            'X-T22-App-End': '123.01825',
            'X-T22-App-Start-UTC': '2026-11-01T04:30:00.100000Z',
            'X-T22-App-End-UTC': '2026-11-01T04:30:00.118250Z',
            'X-T22-Profile-Scope': 'through-start-response; iterable-excluded',
        }

        self.assertEqual({
            'app_ms': 18.25, 'sql_ms': 6.5, 'commit_ms': 1.25, 'queue_ms': 0.75,
            'sql_count': 9, 'server_pid': 4120, 'process_cpu_seconds': 18.75,
            'app_start_monotonic': 123.0, 'app_end_monotonic': 123.01825,
            'app_start_utc': '2026-11-01T04:30:00.100000Z',
            'app_end_utc': '2026-11-01T04:30:00.118250Z',
            'scope': 'through-start-response; iterable-excluded',
        }, strict_load.parse_server_profile(headers))

    def test_absent_or_invalid_profile_headers_are_ignored(self):
        self.assertEqual({}, strict_load.parse_server_profile({}))
        self.assertEqual({'app_ms': 2.0}, strict_load.parse_server_profile({
            'Server-Timing': 'app;dur=2, sql;dur=invalid',
            'X-T22-SQL-Count': 'not-a-count',
        }))

    def test_summary_reports_profiled_samples_separately(self):
        row = {'kind': 'query', 'started': 1.0, 'ended': 1.2, 'ok': True,
               'server_profile': {'queue_ms': 10.0, 'app_ms': 20.0,
                                  'scope': 'through-start-response; iterable-excluded'}}
        summary = strict_load.summarize([row], [], 0.0, 10.0, 0)

        self.assertEqual(1, summary['groups']['query']['profiled_count'])
        self.assertEqual([row['server_profile']['scope']],
                         summary['groups']['query']['profile_scopes'])
        self.assertEqual(20.0,
                         summary['groups']['query']['server_profile']['app_ms']['p95_ms'])


class StreamingArtifactTests(unittest.TestCase):
    def test_request_result_is_flushed_while_run_is_active(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / 'round'
            origin = datetime(2026, 10, 31, 5, 44, 6, tzinfo=timezone.utc)
            writer = strict_load.JsonlRunRecorder(prefix)
            writer.set_clock(100.0, origin)
            try:
                writer.request({'request_id': 1, 'started': 100.1, 'first': 100.2,
                                'ended': 100.3, 'kind': 'query', 'ok': True})
                writer.sample('inflight', {'seconds': 0.2, 'in_flight': 360})
                writer.sample('resources', {'seconds': 0.2, 'host_cpu_percent': 10})
                path = prefix.with_suffix('.requests.jsonl')
                sample_paths = [prefix.with_suffix('.inflight.jsonl'),
                                prefix.with_suffix('.resources.jsonl')]
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if all(item.exists() and item.stat().st_size for item in [path, *sample_paths]):
                        break
                    time.sleep(0.05)
                rows = path.read_text(encoding='utf-8').splitlines()
                self.assertEqual(1, len(rows))
                row = json.loads(rows[0])
                self.assertAlmostEqual(0.1, row['started'])
                self.assertEqual('2026-10-31T05:44:06.100Z', row['started_at_utc'])
                self.assertEqual(360, json.loads(sample_paths[0].read_text(encoding='utf-8'))['in_flight'])
                self.assertEqual(10, json.loads(sample_paths[1].read_text(encoding='utf-8'))['host_cpu_percent'])
            finally:
                writer.close()

    def test_existing_artifact_is_not_truncated(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / 'round'
            path = prefix.with_suffix('.requests.jsonl')
            path.write_text('keep me\n', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                strict_load.JsonlRunRecorder(
                    prefix)
            self.assertEqual('keep me\n', path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
