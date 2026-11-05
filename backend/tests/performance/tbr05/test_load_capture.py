import hashlib
import importlib.util
import random
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).with_name('03_tbr05_load.py')
SPEC = importlib.util.spec_from_file_location('tbr05_load', SCRIPT)
load = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {'requests': types.ModuleType('requests'),
                              'psutil': types.ModuleType('psutil')}):
    SPEC.loader.exec_module(load)


class Response:
    status_code = 200
    headers = {}

    def iter_content(self, chunk_size):
        yield b'{"items":[]}'

    def close(self):
        pass


class Session:
    def request(self, *args, **kwargs):
        return Response()


class LoadCaptureTests(unittest.TestCase):
    def test_adoption_continues_past_old_capacity_without_reusing_suggestions(self):
        rng = random.Random(20261024)
        with patch.object(load, 'next_target', side_effect=[0, 6664, 6665, load.ADOPTION_TARGETS - 1]):
            requests = [load.select_request('write', 4, rng) for _ in range(4)]
        payloads = [request[3] for request in requests]
        self.assertEqual(payloads[0]['record_ref'], payloads[2]['record_ref'])
        self.assertNotEqual(payloads[0]['suggestion_ref'], payloads[2]['suggestion_ref'])
        self.assertEqual(4, len({p['suggestion_ref'] for p in payloads}))
        self.assertEqual(load.ref('record', 4, 3998), payloads[-1]['record_ref'])
        for request in requests:
            self.assertIn(request[3]['suggestion_ref'], request[2])
            self.assertEqual('json', request[4])
        with patch.object(load, 'next_target', return_value=load.ADOPTION_TARGETS):
            with self.assertRaisesRegex(RuntimeError, 'pool exhausted'):
                load.select_request('write', 4, rng)

    def test_target_base_pool_round_robins_all_workers(self):
        args = types.SimpleNamespace(target_base=[f'http://app:{18930 + port}/' for port in range(8)],
                                     tunnel_base=[], base='http://unused')

        bases = load.resolve_target_bases(args)

        self.assertEqual([f'http://app:{18930 + port}' for port in range(8)], bases)
        self.assertEqual([45] * 8, load.target_worker_assignments(360, len(bases)))

    def test_legacy_tunnel_base_alias_is_preserved(self):
        args = types.SimpleNamespace(target_base=[], tunnel_base=['http://app:18930', 'http://app:18931/'],
                                     base='http://unused')

        self.assertEqual(['http://app:18930', 'http://app:18931'], load.resolve_target_bases(args))

    def test_target_and_tunnel_base_options_cannot_be_mixed(self):
        args = types.SimpleNamespace(target_base=['http://app:18930'], tunnel_base=['http://app:18931'],
                                     base='http://unused')

        with self.assertRaisesRegex(ValueError, 'either --target-base or legacy --tunnel-base'):
            load.resolve_target_bases(args)

    def test_http_inflight_ends_before_response_validation(self):
        with patch.object(load, 'select_request', return_value=('doctor', 'GET', '/api', None, 'json', None)):
            with patch.object(load.json, 'loads', side_effect=AssertionError('validated before HTTP completed')):
                capture = load.run_one(Session(), 'http://test', {'doctor': {}}, {}, 1, 'query', None)

        self.assertEqual(0, load.ACTIVE)
        self.assertLess(capture[0]['started'], capture[0]['ended'])
        self.assertTrue(load.validate_capture(capture)['ok'])

    def test_enqueue_wait_is_available_before_validation(self):
        pending = load.ValidationQueue(maxsize=1)
        row = {}
        with patch.object(load.time, 'perf_counter', side_effect=[1.0, 1.125]):
            pending.submit((row, b'', {}, None))
        self.assertEqual(125.0, pending.get_nowait()[0]['validation_enqueue_wait_ms'])
        stop = object()
        pending.put_nowait(stop)
        self.assertIs(stop, pending.get_nowait())

    def test_export_phase_timings_survive_header_parsing(self):
        headers = {'Server-Timing': 'export-snapshot;dur=12.5, export-csv;dur=3, '
                                    'export-audit-targets;dur=8, commit;dur=2'}
        self.assertEqual({'export_snapshot_ms': 12.5, 'export_csv_ms': 3,
                          'export_audit_targets_ms': 8, 'commit_ms': 2},
                         load.parse_server_profile(headers))

    def test_export_validation_checks_complete_bytes_after_http_timing(self):
        body = b'event_ref,record_ref\r\na,b\r\n'
        digest = hashlib.sha256(body).hexdigest()
        row = {'request_id': 2, 'kind': 'detail_export', 'status': 200,
               'expected_count': 1, 'started': 1.0, 'ended': 1.1, 'bytes': len(body)}
        headers = {'Content-Length': str(len(body)), 'X-Byte-Count': str(len(body)),
                   'X-Content-SHA256': digest, 'X-Record-Count': '1'}

        result = load.validate_capture((row, body, headers, None))

        self.assertTrue(result['ok'])
        self.assertEqual(1, result['rows'])
        self.assertEqual(1.1, result['ended'])


if __name__ == '__main__':
    unittest.main()
