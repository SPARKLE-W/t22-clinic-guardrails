import random
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from test_load_capture import load, Session


class PrepareAheadTests(unittest.TestCase):
    def test_failed_following_preparation_keeps_completed_capture(self):
        captured = []
        initial = {'number': 1, 'kind': 'query', 'selection': None, 'selection_ms': 0.,
                   'task_selection_ms': 0.}
        session = type('Session', (), {'close': lambda self: None})()
        with patch.object(load.requests, 'Session', return_value=session, create=True), \
                patch.object(load, 'prepare_task', side_effect=[initial, ValueError('synthetic planner failure')]), \
                patch.object(load, 'run_one', return_value=({'ended': 0.}, b'{}', {}, None)), \
                ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaisesRegex(ValueError, 'synthetic planner failure'):
                load.worker(0, type('Gate', (), {'wait': lambda self, timeout: None})(),
                            time.perf_counter() + 10, 'http://test', {}, {},
                            type('Pending', (), {'submit': staticmethod(captured.append)})(), pool)
        self.assertEqual(1, len(captured))
        self.assertIn('client_prepare_wait_ms', captured[0][0])

    def test_preparation_preserves_selection_and_random_state(self):
        for kind in ('query', 'clinical_export', 'detail_export'):
            actual_rng = random.Random(20261024)
            expected_rng = random.Random(20261024)
            for number in range(40):
                expected = load.select_request(kind, number, expected_rng)
                with patch.object(load, 'next_task', return_value=(number, kind)):
                    actual = load.prepare_task(actual_rng)
                self.assertEqual(expected, actual['selection'])
                self.assertEqual(expected_rng.getstate(), actual_rng.getstate())

    def test_version_is_refreshed_at_dispatch_without_selecting_again(self):
        key = (0, 17)
        selection = ('doctor', 'POST', '/api', {'original_version_ref': 'old', '_load_target_key': key}, 'json', None)
        prepared = {'selection': selection, 'selection_ms': 12.0, 'started': 0.0, 'ended': .012}
        session = Session()
        with patch.object(load, 'current_version_ref', return_value='new'), \
                patch.object(load, 'select_request', side_effect=AssertionError('second selection')), \
                patch.object(session, 'request', wraps=session.request) as request:
            capture = load.run_one(session, 'http://test', {'doctor': {'csrftoken': 'synthetic'}},
                                   {}, 1, 'guard', None, prepared=prepared)
        self.assertEqual('new', request.call_args.kwargs['json']['original_version_ref'])
        self.assertEqual(12.0, capture[0]['client_select_ms'])
        self.assertLess(capture[0]['started'], capture[0]['ended'])

    def test_following_preparation_overlaps_request_and_stays_bounded(self):
        planning = threading.Event()
        plans = []
        captured = []
        def prepare(rng):
            item = {'number': len(plans), 'kind': 'query', 'selection': None,
                    'selection_ms': 0., 'task_selection_ms': 0.}
            plans.append(item)
            if len(plans) == 2:
                planning.set()
            return item
        def request(*args, **kwargs):
            self.assertTrue(planning.wait(2))
            return ({'ended': time.perf_counter()}, b'{}', {}, None)
        session = type('Session', (), {'close': lambda self: None})()
        clock = [0.0]
        def submit(capture):
            captured.append(capture)
            clock[0] = 2.0
        with patch.object(load.requests, 'Session', return_value=session, create=True), \
                patch.object(load, 'prepare_task', side_effect=prepare), \
                patch.object(load, 'run_one', side_effect=request), \
                patch.object(load.time, 'perf_counter', side_effect=lambda: clock[0]), \
                ThreadPoolExecutor(max_workers=1) as pool:
            unused = load.worker(0, type('Gate', (), {'wait': lambda self, timeout: None})(), 1.0,
                                 'http://test', {}, {}, type('Pending', (), {'submit': staticmethod(submit)})(), pool)
        self.assertEqual(1, len(captured))
        self.assertEqual(2, len(plans))
        self.assertEqual({'request_id': 1, 'kind': 'query'}, unused)
        self.assertTrue(captured[0][0]['client_preparation_ahead'])


if __name__ == '__main__':
    unittest.main()
