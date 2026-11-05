import hashlib
import importlib.util
import json
import tempfile
from datetime import datetime, timezone
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


class SwitchIntervalTests(unittest.TestCase):
    def test_candidate_applies_only_inside_scope_and_restores_on_error(self):
        original = sys.getswitchinterval()
        with self.assertRaisesRegex(RuntimeError, 'synthetic'):
            with load.gil_switch_interval(1.0) as active:
                self.assertAlmostEqual(.001, active)
                self.assertAlmostEqual(.001, sys.getswitchinterval())
                raise RuntimeError('synthetic')
        self.assertEqual(original, sys.getswitchinterval())

    def test_default_preserves_existing_interval(self):
        original = sys.getswitchinterval()
        with patch.object(sys, 'setswitchinterval', side_effect=AssertionError('default changed')):
            with load.gil_switch_interval(None) as active:
                self.assertEqual(original, active)
        self.assertEqual(original, sys.getswitchinterval())

    def test_invalid_intervals_fail_before_changing_runtime(self):
        original = sys.getswitchinterval()
        for value in (0, -1, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                with load.gil_switch_interval(value):
                    self.fail('invalid interval entered')
        self.assertEqual(original, sys.getswitchinterval())


class GCPauseCaptureTests(unittest.TestCase):
    def recorder(self, folder, enabled=True):
        return load.GCPauseRecorder(Path(folder) / 'run', 100.0,
                                    datetime(2026, 11, 6, tzinfo=timezone.utc), enabled)

    def test_gc_events_share_request_clock_without_callback_file_io(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = self.recorder(folder)
            with patch.object(load.gc, 'callbacks', []), recorder:
                with patch.object(load.time, 'perf_counter', side_effect=[101.0, 101.048]):
                    with patch.object(load.json, 'dump', side_effect=AssertionError('callback file IO')):
                        recorder.callback('start', {'generation': 2})
                        recorder.callback('stop', {'generation': 2, 'collected': 7, 'uncollectable': 0})
            rows = json.loads((Path(folder) / 'run.gc.json').read_text())
            self.assertEqual(['start', 'stop'], [r['phase'] for r in rows])
            self.assertEqual([1.0, 1.048], [round(r['t'], 3) for r in rows])
            self.assertEqual(7, rows[1]['collected'])
            self.assertEqual(2, rows[0]['generation'])
            self.assertEqual('2026-11-06T00:00:01', rows[0]['timestamp_utc'][:19])

    def test_exception_cleanup_preserves_other_callbacks_and_gc_settings(self):
        other = lambda phase, info: None
        with tempfile.TemporaryDirectory() as folder:
            recorder = self.recorder(folder)
            before = load.gc.get_threshold(), load.gc.isenabled()
            with patch.object(load.gc, 'callbacks', [other]):
                with self.assertRaisesRegex(RuntimeError, 'worker failed'):
                    with recorder:
                        self.assertIn(recorder.callback, load.gc.callbacks)
                        raise RuntimeError('worker failed')
                self.assertEqual([other], load.gc.callbacks)
            self.assertEqual(before, (load.gc.get_threshold(), load.gc.isenabled()))
            self.assertEqual([], json.loads((Path(folder) / 'run.gc.json').read_text()))

    def test_real_collection_emits_start_stop_and_preserves_callback_registry(self):
        with tempfile.TemporaryDirectory() as folder:
            callbacks = list(load.gc.callbacks)
            with self.recorder(folder):
                load.gc.collect(2)
            self.assertEqual(callbacks, load.gc.callbacks)
            rows = json.loads((Path(folder) / 'run.gc.json').read_text())
            generation_two = [r for r in rows if r['generation'] == 2]
            self.assertEqual(['start', 'stop'], [r['phase'] for r in generation_two])
            self.assertLessEqual(generation_two[0]['t'], generation_two[1]['t'])

    def test_disabled_capture_leaves_no_file_or_callback(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(load.gc, 'callbacks', []):
                with self.recorder(folder, enabled=False):
                    self.assertEqual([], load.gc.callbacks)
            self.assertEqual([], list(Path(folder).iterdir()))

    def test_existing_gc_evidence_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'run.gc.json'
            path.write_text('original')
            with patch.object(load.gc, 'callbacks', []):
                with self.assertRaises(FileExistsError):
                    with self.recorder(folder):
                        pass
                self.assertEqual([], load.gc.callbacks)
            self.assertEqual('original', path.read_text())


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
    def test_request_identifiers_are_unique_without_per_request_entropy(self):
        with patch.object(load.uuid, 'uuid4', side_effect=AssertionError('entropy in request loop')):
            refs = {load.request_ref(purpose, n) for purpose in ('pet', 'idempotency') for n in range(10000)}
            self.assertEqual(20000, len(refs))
            first = load.request_ref('pet', 1)
            self.assertEqual(first, load.select_request('reception', 1, random.Random(4))[3]['pet_ref'])
            self.assertEqual(5, load.uuid.UUID(first).version)
            with patch.object(load, 'REQUEST_NAMESPACE', load.uuid.UUID(int=0)):
                self.assertNotEqual(first, load.request_ref('pet', 1))

    def test_export_reference_cache_matches_fixed_fixture(self):
        for (kind, shop), refs in load.EXPORT_REFS.items():
            self.assertEqual(4000, len(refs))
            self.assertEqual(tuple(load.ref(kind, shop, n) for n in range(4000)), refs)

    def test_cached_exports_preserve_payload_and_rng_state(self):
        class UncachedRefs:
            def __init__(self, kind, shop):
                self.kind, self.shop = kind, shop

            def __getitem__(self, number):
                return load.ref(self.kind, self.shop, number)

        uncached = {key: UncachedRefs(*key) for key in load.EXPORT_REFS}
        for kind in ('clinical_export', 'detail_export'):
            cached_rng = random.Random(20261024)
            original_rng = random.Random(20261024)
            for sequence in range(100):
                cached = load.select_request(kind, sequence, cached_rng)
                with patch.object(load, 'EXPORT_REFS', uncached):
                    original = load.select_request(kind, sequence, original_rng)
                self.assertEqual(original, cached)
                self.assertEqual(original_rng.getstate(), cached_rng.getstate())

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

    def test_preparation_timings_do_not_shift_http_interval(self):
        with patch.object(load, 'select_request', return_value=('doctor', 'GET', '/api', None, 'json', None)):
            with patch.object(load.time, 'perf_counter', side_effect=[1, 1.1, 1.2, 1.3, 1.4, 1.7]):
                row = load.run_one(Session(), 'http://test', {'doctor': {}}, {}, 1, 'query', None)[0]
        self.assertAlmostEqual(100, row['client_select_ms'])
        self.assertAlmostEqual(100, row['client_headers_ms'])
        self.assertEqual(1.2, row['started'])
        self.assertEqual(1.3, row['first'])
        self.assertEqual(1.4, row['ended'])
        self.assertAlmostEqual(300, row['client_response_finalize_ms'])
        self.assertEqual(0, load.ACTIVE)

    def test_preparation_timings_survive_transport_failure(self):
        with patch.object(load, 'select_request', return_value=('doctor', 'GET', '/api', None, 'json', None)):
            with patch.object(Session, 'request', side_effect=OSError('synthetic')):
                with patch.object(load.time, 'perf_counter', side_effect=[1, 1.1, 1.2, 1.4, 1.7]):
                    row = load.run_one(Session(), 'http://test', {'doctor': {}}, {}, 1, 'query', None)[0]
        self.assertFalse(row['ok'])
        self.assertAlmostEqual(100, row['client_headers_ms'])
        self.assertEqual(1.2, row['started'])
        self.assertEqual(1.4, row['ended'])
        self.assertEqual(0, load.ACTIVE)

    def test_worker_records_task_and_inline_validation_outside_http_interval(self):
        row = {'kind': 'guard', 'started': .003, 'ended': .0035}
        captures = []
        session = types.SimpleNamespace(close=lambda: None)
        gate = types.SimpleNamespace(wait=lambda **kwargs: None)
        pending = types.SimpleNamespace(submit=captures.append)
        with patch.object(load.requests, 'Session', return_value=session, create=True), \
             patch.object(load, 'next_task', return_value=(1, 'guard')), \
             patch.object(load, 'run_one', return_value=(row, b'{}', {}, None)), \
             patch.object(load, 'validate_capture', side_effect=lambda capture: capture[0]), \
             patch.object(load.time, 'perf_counter', side_effect=[0, .001, .003, .004, .009, 2]):
            load.worker(0, gate, 1, 'http://test', {}, {}, pending)
        self.assertEqual(1, len(captures))
        self.assertAlmostEqual(2, row['client_task_selection_ms'])
        self.assertAlmostEqual(5, row['client_inline_validation_ms'])
        self.assertEqual(.0035, row['ended'])

    def test_export_phase_timings_survive_header_parsing(self):
        headers = {'Server-Timing': 'export-snapshot;dur=12.5, export-csv;dur=3, '
                                    'export-audit-targets;dur=8, commit;dur=2'}
        self.assertEqual({'export_snapshot_ms': 12.5, 'export_csv_ms': 3,
                          'export_audit_targets_ms': 8, 'commit_ms': 2},
                         load.parse_server_profile(headers))

    def test_connection_profile_keeps_numeric_diagnostics_only(self):
        headers = {'Server-Timing': 'db-connect;dur=125.5, sql;dur=0',
                   'X-T22-DB-Connect-Count': '1', 'X-T22-DB-Connect-Error-Code': '2003'}
        self.assertEqual({'db_connect_ms': 125.5, 'sql_ms': 0,
                          'db_connect_count': 1, 'db_connect_error_code': 2003},
                         load.parse_server_profile(headers))
        headers['X-T22-DB-Connect-Error-Code'] = 'private error text'
        self.assertNotIn('db_connect_error_code', load.parse_server_profile(headers))

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


class LoadSummaryWindowTests(unittest.TestCase):
    @staticmethod
    def row(started, ended, ok=True):
        return {'kind': 'query', 'started': started, 'ended': ended, 'ok': ok}

    def test_short_measurement_excludes_drain_and_extra_minutes(self):
        rows = [self.row(161, 165), self.row(221, 225, False), self.row(279, 281)]
        report = load.summarize(rows, [], 100, 280, 60)
        self.assertEqual([
            {'minute': 1, 'completed': 1, 'success': 1, 'failures': 0},
            {'minute': 2, 'completed': 1, 'success': 0, 'failures': 1},
        ], report['minutes'])
        self.assertEqual(3, report['formal_requests_started'])

    def test_partial_minute_has_explicit_duration_and_closed_end(self):
        rows = [self.row(281, 284), self.row(282, 285)]
        report = load.summarize(rows, [], 100, 285, 60)
        self.assertEqual(3, len(report['minutes']))
        self.assertEqual({'minute': 3, 'completed': 1, 'success': 1, 'failures': 0,
                          'duration_seconds': 5}, report['minutes'][-1])

    def test_frozen_full_window_preserves_ten_full_minute_shape(self):
        rows = [self.row(1300, 1300.1), self.row(1899, 1900)]
        report = load.summarize(rows, [], 1000, 1900, 300)
        self.assertEqual(10, len(report['minutes']))
        self.assertTrue(all(set(row) == {'minute', 'completed', 'success', 'failures'}
                            for row in report['minutes']))
        self.assertEqual(1, sum(row['success'] for row in report['minutes']))


if __name__ == '__main__':
    unittest.main()
