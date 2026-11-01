import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name('22_服务器资源与请求对齐.py')
SPEC = importlib.util.spec_from_file_location('resource_alignment', SCRIPT)
alignment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(alignment)


class ResourceRequestAlignmentTests(unittest.TestCase):
    def write_jsonl(self, path, rows):
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    def test_aligns_profiled_requests_resources_and_process_deltas_by_utc_minute(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requests_path, resources_path = root / 'requests.jsonl', root / 'resources.jsonl'
            self.write_jsonl(requests_path, [
                {'kind': 'query', 'ok': True, 'started': 1.0, 'ended': 1.1,
                 'server_profile': {'app_start_utc': '2026-11-01T12:30:15+08:00',
                                    'queue_ms': 10, 'app_ms': 20, 'sql_ms': 5, 'commit_ms': 1}},
                {'kind': 'query', 'ok': True, 'started': 2.0, 'ended': 2.4,
                 'server_profile': {'app_start_utc': '2026-11-01T04:30:50Z',
                                    'queue_ms': 30, 'app_ms': 25, 'sql_ms': 8, 'commit_ms': 2}},
                {'kind': 'guard', 'ok': False, 'started': 3.0, 'ended': 3.5,
                 'server_profile': {'app_start_utc': '2026-11-01T04:31:10Z',
                                    'queue_ms': 40, 'app_ms': 50}},
                {'kind': 'write', 'ok': False, 'started': 4.0, 'ended': 4.2},
            ])
            process_sample_1 = [
                {'pid': 101, 'name': 'python', 'cpu_cumulative_seconds': 10.0},
                {'pid': 202, 'name': 'mysqld', 'cpu_cumulative_seconds': 4.0},
            ]
            process_sample_2 = [
                {'pid': 101, 'name': 'python', 'cpu_cumulative_seconds': 10.5},
                {'pid': 202, 'name': 'mysqld', 'cpu_cumulative_seconds': 4.25},
            ]
            physical_disks = lambda writes, latency, queue: [
                {'instance': '0 C:', 'write_iops': writes, 'read_iops': 2,
                 'average_write_latency_ms': latency, 'average_read_latency_ms': 1, 'queue_length': queue},
                {'instance': '_Total', 'write_iops': writes, 'read_iops': 2,
                 'average_write_latency_ms': latency, 'average_read_latency_ms': 1, 'queue_length': queue},
            ]
            self.write_jsonl(resources_path, [
                {'timestamp_utc': '2026-11-01T04:30:10Z', 'sampling_error': None,
                 'host_cpu_percent': 70, 'available_memory_bytes': 100,
                 'processes': process_sample_1, 'physical_disks': physical_disks(10, 1, 1)},
                {'timestamp_utc': '2026-11-01T04:30:40Z', 'sampling_error': None,
                 'host_cpu_percent': 90, 'available_memory_bytes': 80,
                 'processes': process_sample_2, 'physical_disks': physical_disks(20, 3, 2)},
            ])

            columns, rows, summary = alignment.build_aligned_rows(requests_path, resources_path)

        self.assertEqual(4, summary['request_count'])
        self.assertEqual(1, summary['unmatched_requests'])
        self.assertEqual(1, summary['resource_minute_count'])
        self.assertIn('query_app_p95_ms', columns)
        first, second = rows
        self.assertEqual('2026-11-01T04:30Z', first['utc_minute'])
        self.assertEqual(2, first['query_count'])
        self.assertEqual(400.0, first['query_e2e_p95_ms'])
        self.assertEqual(25.0, first['query_app_p95_ms'])
        self.assertEqual(0.5, first['python_cpu_delta_seconds'])
        self.assertEqual(0.25, first['mysql_cpu_delta_seconds'])
        self.assertEqual(15.0, first['disk_write_iops_mean'])
        self.assertEqual(80.0, first['host_cpu_mean_percent'])
        self.assertEqual(80.0, first['available_memory_min_bytes'])
        self.assertEqual('2026-11-01T04:31Z', second['utc_minute'])
        self.assertEqual(1, second['guard_failures'])

    def test_csv_output_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'aligned.csv'
            output.write_text('preserve\n', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                alignment.write_csv(output, ['utc_minute'], [])
            self.assertEqual('preserve\n', output.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
