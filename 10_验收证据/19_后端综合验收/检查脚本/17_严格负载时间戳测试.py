import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).with_name('07_正式HTTP负载.py')
SPEC = importlib.util.spec_from_file_location('strict_load', SCRIPT)
strict_load = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(strict_load)


class AbsoluteTimestampTests(unittest.TestCase):
    def test_perf_counter_offset_maps_to_utc_event_time(self):
        origin = datetime(2026, 10, 31, 5, 44, 6, tzinfo=timezone.utc)
        self.assertEqual('2026-10-31T05:50:07.000Z', strict_load.utc_at(origin, 361))

    def test_request_timestamps_use_perf_counter_offsets(self):
        origin = datetime(2026, 10, 31, 5, 44, 6, tzinfo=timezone.utc)
        row = {'started': 861.0, 'first': 861.025, 'ended': 861.3}

        self.assertEqual({
            'started_at_utc': '2026-10-31T05:58:27.000Z',
            'first_byte_at_utc': '2026-10-31T05:58:27.025Z',
            'ended_at_utc': '2026-10-31T05:58:27.300Z',
        }, strict_load.request_timestamps(row, 0.0, origin))

    def test_request_without_first_byte_keeps_null_timestamp(self):
        origin = datetime(2026, 10, 31, 5, 44, 6, tzinfo=timezone.utc)
        row = {'started': 300.0, 'first': None, 'ended': 301.0}

        self.assertIsNone(strict_load.request_timestamps(row, 0.0, origin)['first_byte_at_utc'])


if __name__ == '__main__':
    unittest.main()
