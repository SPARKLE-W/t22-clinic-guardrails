import gc
import weakref
import unittest
from concurrent.futures import ThreadPoolExecutor

from test_load_capture import load


class RequestStatisticsTests(unittest.TestCase):
    def rows(self):
        kinds = sorted(set(load.MIX))
        rows = []
        for i in range(600):
            kind = kinds[i % len(kinds)]
            started = 157 + i * .2
            row = {'kind': kind, 'started': started, 'ended': started + (i % 11) / 1000,
                   'ok': i % 17 != 0, 'tier': ('1-20', '50-200', '1000-4000')[i % 3],
                   'server_profile': {}}
            if i % 3:
                row['server_profile'] = {'scope': 'through-start-response',
                                        'sql_ms': i / 10, 'commit_ms': i / 100}
            rows.append(row)
        rows += [{'kind': 'query', 'started': 159.9, 'ended': 160.1, 'ok': True},
                 {'kind': 'query', 'started': 284.9, 'ended': 285.1, 'ok': False},
                 {'kind': 'query', 'started': 285, 'ended': 285.1, 'ok': True}]
        return rows

    def test_exact_legacy_summary_including_warmup_tail_failures_and_partial_minutes(self):
        rows = self.rows()
        samples = [{'seconds': 60, 'in_flight': 357}, {'seconds': 184.9, 'in_flight': 360}]
        stats = load.RequestStatistics(100, 285, 60)
        for row in rows:
            stats.append(row)
        self.assertEqual(load.summarize(rows, samples, 100, 285, 60), stats.summarize(samples))

    def test_empty_groups_and_empty_window_match_legacy(self):
        stats = load.RequestStatistics(0, 60, 60)
        self.assertEqual(load.summarize([], [], 0, 60, 60), stats.summarize([]))

    def test_parallel_validation_keeps_exact_counts_and_quantiles(self):
        rows = self.rows()
        stats = load.RequestStatistics(100, 285, 60)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(stats.append, rows))
        self.assertEqual(load.summarize(rows, [], 100, 285, 60), stats.summarize([]))

    def test_request_dictionaries_are_not_retained(self):
        class Row(dict):
            pass
        stats = load.RequestStatistics(0, 600, 0)
        row = Row(kind='query', started=1, ended=2, ok=True, unused='x' * 100000)
        reference = weakref.ref(row)
        stats.append(row)
        del row
        gc.collect()
        self.assertIsNone(reference())
        self.assertEqual(1, stats.summarize([])['formal_requests_started'])


if __name__ == '__main__':
    unittest.main()
