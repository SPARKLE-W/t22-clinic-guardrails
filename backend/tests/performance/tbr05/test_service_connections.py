import importlib.util
import io
import runpy
import sys
import types
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    'tbr05_service_connections', Path(__file__).with_name('02_formal_service.py'))
service = importlib.util.module_from_spec(SPEC)
wsgi = types.ModuleType('django.core.wsgi')
wsgi.get_wsgi_application = lambda: None
waitress = types.ModuleType('waitress')
waitress.serve = lambda *args, **kwargs: None
with patch.dict('os.environ', {'T22_FORMAL_LOAD': '1'}), patch.dict(
        sys.modules, {'django.core.wsgi': wsgi, 'waitress': waitress}):
    SPEC.loader.exec_module(service)


class Database:
    def get_new_connection(self, params):
        return params['result']


class SchedulingCandidateTests(unittest.TestCase):
    def test_candidate_applies_and_restores_on_failure(self):
        original = sys.getswitchinterval()
        with self.assertRaisesRegex(RuntimeError, 'synthetic'):
            with service.gil_switch_interval(.1) as active:
                self.assertAlmostEqual(.1, active)
                self.assertAlmostEqual(.0001, sys.getswitchinterval())
                raise RuntimeError('synthetic')
        self.assertEqual(original, sys.getswitchinterval())

    def test_default_does_not_change_interpreter(self):
        original = sys.getswitchinterval()
        with patch.object(sys, 'setswitchinterval', side_effect=AssertionError('default changed')):
            with service.gil_switch_interval(None) as active:
                self.assertAlmostEqual(original * 1000, active)

    def test_service_entry_uses_environment_and_cli_override_only_while_serving(self):
        original = sys.getswitchinterval()
        for extra, expected in [([], .2), (['--gil-switch-interval-ms', '.1'], .1)]:
            observed = []
            fake_waitress = types.ModuleType('waitress')
            fake_waitress.serve = lambda *a, **kw: observed.append(sys.getswitchinterval() * 1000)
            output = io.StringIO()
            with self.subTest(extra=extra), patch.dict('os.environ', {
                    'T22_FORMAL_LOAD': '1', 'T22_SERVER_GIL_SWITCH_INTERVAL_MS': '.2'}), patch.dict(
                    sys.modules, {'django.core.wsgi': wsgi, 'waitress': fake_waitress}), patch.object(
                    sys, 'argv', ['service', *extra]), redirect_stdout(output):
                runpy.run_path(str(Path(__file__).with_name('02_formal_service.py')), run_name='__main__')
            self.assertAlmostEqual(expected, observed[0])
            self.assertIn('GIL_SWITCH_INTERVAL_MS=', output.getvalue())
            self.assertEqual(original, sys.getswitchinterval())

    def test_invalid_candidate_rejected_before_serving(self):
        original = sys.getswitchinterval()
        for value in (0, -1, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                with service.gil_switch_interval(value):
                    self.fail('invalid value accepted')
        self.assertEqual(original, sys.getswitchinterval())


class OutputBufferTests(unittest.TestCase):
    def run_entry(self, extra):
        observed = []
        fake_waitress = types.ModuleType('waitress')
        fake_waitress.serve = lambda *a, **kw: observed.append(kw)
        with patch.dict('os.environ', {'T22_FORMAL_LOAD': '1'}), patch.dict(
                sys.modules, {'django.core.wsgi': wsgi, 'waitress': fake_waitress}), patch.object(
                sys, 'argv', ['service', *extra]), redirect_stdout(io.StringIO()):
            runpy.run_path(str(Path(__file__).with_name('02_formal_service.py')), run_name='__main__')
        return observed

    def test_default_and_candidate_reach_server_configuration(self):
        for extra, expected in [([], 1048576), (['--outbuf-overflow-bytes', '8388608'], 8388608)]:
            with self.subTest(extra=extra):
                self.assertEqual(expected, self.run_entry(extra)[0]['outbuf_overflow'])

    def test_profiled_server_uses_the_same_candidate(self):
        captured = {}
        dispatcher = type('Dispatcher', (), {'set_thread_count': lambda self, count: None})

        def create_server(app, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(run=lambda: None)

        modules = {
            'django.db': types.SimpleNamespace(connections={}),
            'waitress.channel': types.SimpleNamespace(HTTPChannel=object),
            'waitress.server': types.SimpleNamespace(create_server=create_server),
            'waitress.task': types.SimpleNamespace(ThreadedTaskDispatcher=dispatcher)}
        with patch.dict(sys.modules, modules):
            service.serve_profiled(18930, 48, '127.0.0.1', outbuf_overflow=8388608)
        self.assertEqual(8388608, captured['outbuf_overflow'])

    def test_nonpositive_threshold_is_rejected_before_serving(self):
        for value in ('0', '-1'):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self.run_entry(['--outbuf-overflow-bytes', value])


class ConnectionProfilingTests(unittest.TestCase):
    def test_success_counts_attempt_and_restores_method(self):
        database, counters = Database(), {}
        result = object()
        with patch.object(service.time, 'perf_counter', side_effect=[1.0, 1.125]):
            with service.measure_connections(database, counters):
                self.assertIs(result, database.get_new_connection({'result': result}))
        self.assertEqual({'db_connect_ms': 125.0, 'db_connect_count': 1}, counters)
        self.assertNotIn('get_new_connection', database.__dict__)

    def test_numeric_failure_is_measured_and_original_error_propagates(self):
        database, counters = Database(), {}
        failure = RuntimeError(2003, 'private connection details must not be captured')
        original = lambda params: (_ for _ in ()).throw(failure)
        database.get_new_connection = original
        with patch.object(service.time, 'perf_counter', side_effect=[1.0, 3.0]):
            with self.assertRaises(RuntimeError) as caught:
                with service.measure_connections(database, counters):
                    database.get_new_connection({'password': 'private'})
        self.assertIs(failure, caught.exception)
        self.assertIs(original, database.get_new_connection)
        self.assertEqual({'db_connect_ms': 2000.0, 'db_connect_count': 1,
                          'db_connect_error_code': 2003}, counters)

    def test_non_numeric_exception_details_are_not_recorded(self):
        database, counters = Database(), {}
        database.get_new_connection = lambda params: (_ for _ in ()).throw(ValueError('private'))
        with self.assertRaises(ValueError):
            with service.measure_connections(database, counters):
                database.get_new_connection({})
        self.assertNotIn('db_connect_error_code', counters)
        self.assertEqual(1, counters['db_connect_count'])

    def test_reused_connection_has_no_new_connection_cost(self):
        database, counters = Database(), {}
        with service.measure_connections(database, counters):
            pass
        self.assertEqual({'db_connect_ms': 0.0, 'db_connect_count': 0}, counters)

    def test_profile_headers_capture_connection_failure_before_any_sql(self):
        database, captured = Database(), {}
        database._commit = lambda: None
        database.execute_wrapper = lambda wrapper: nullcontext()
        database.get_new_connection = lambda params: (_ for _ in ()).throw(RuntimeError(2003, 'private'))

        def application(environ, start_response):
            try:
                database.get_new_connection({'password': 'private'})
            except RuntimeError:
                start_response('503 Service Unavailable', [])
                return [b'unconfirmed']

        def create_server(app, **kwargs):
            def run():
                def start_response(status, headers, exc_info=None):
                    captured.update(status=status, headers=dict(headers))
                self.assertEqual([b'unconfirmed'], app({}, start_response))
            return types.SimpleNamespace(run=run)

        dispatcher = type('Dispatcher', (), {'set_thread_count': lambda self, count: None})
        modules = {
            'django.db': types.SimpleNamespace(connections={'default': database}),
            'waitress.channel': types.SimpleNamespace(HTTPChannel=object),
            'waitress.server': types.SimpleNamespace(create_server=create_server),
            'waitress.task': types.SimpleNamespace(ThreadedTaskDispatcher=dispatcher)}
        with patch.dict(sys.modules, modules), patch.object(service, 'get_wsgi_application', return_value=application):
            service.serve_profiled(18930, 48, '127.0.0.1')
        self.assertEqual('503 Service Unavailable', captured['status'])
        self.assertEqual('0', captured['headers']['X-T22-SQL-Count'])
        self.assertEqual('1', captured['headers']['X-T22-DB-Connect-Count'])
        self.assertEqual('2003', captured['headers']['X-T22-DB-Connect-Error-Code'])
        self.assertIn('db-connect;dur=', captured['headers']['Server-Timing'])
        self.assertNotIn('private', str(captured))


if __name__ == '__main__':
    unittest.main()
