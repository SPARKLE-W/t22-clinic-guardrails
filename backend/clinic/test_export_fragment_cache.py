import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from .export_fragment_cache import FragmentCache


class FragmentCacheTests(unittest.TestCase):
    def test_complete_source_key_changes_force_rebuild(self):
        cache = FragmentCache()
        calls = []
        def build(value):
            calls.append(value)
            return {'fragment': value}
        for source in ('original', 'original', 'corrected'):
            self.assertEqual(source, cache.get(('revision', 'record', source), lambda: build(source))['fragment'])
        self.assertEqual(['original', 'corrected'], calls)

    def test_returned_mapping_cannot_change_cached_fragment(self):
        cache = FragmentCache()
        cache.get(('record',), lambda: {'fragment': 'original'})['fragment'] = 'changed'
        self.assertEqual({'fragment': 'original'}, cache.get(('record',), lambda: self.fail('cache miss')))

    def test_budget_evicts_least_recently_used_and_skips_oversized_values(self):
        cache = FragmentCache(limit=2500)
        cache.get(('a',), lambda: {'fragment': 'a' * 400})
        cache.get(('b',), lambda: {'fragment': 'b' * 400})
        cache.get(('a',), lambda: self.fail('a should be cached'))
        cache.get(('c',), lambda: {'fragment': 'c' * 400})
        cache.get(('a',), lambda: self.fail('recent a was evicted'))
        rebuilt = []
        cache.get(('b',), lambda: rebuilt.append('b') or {'fragment': 'b' * 400})
        self.assertEqual(['b'], rebuilt)
        self.assertLessEqual(cache.bytes_used, cache.limit)
        tiny = FragmentCache(limit=100)
        for _ in range(2):
            tiny.get(('large',), lambda: rebuilt.append('large') or {'fragment': 'x' * 1000})
        self.assertEqual(['b', 'large', 'large'], rebuilt)
        self.assertEqual(0, tiny.bytes_used)

    def test_failed_build_is_not_cached(self):
        cache = FragmentCache()
        def fail():
            raise ValueError('synthetic')
        with self.assertRaises(ValueError):
            cache.get(('record',), fail)
        self.assertEqual({'fragment': 'valid'}, cache.get(('record',), lambda: {'fragment': 'valid'}))

    def test_concurrent_misses_charge_one_entry(self):
        cache = FragmentCache()
        barrier = Barrier(4)
        def build():
            barrier.wait(timeout=5)
            return {'fragment': 'same'}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: cache.get(('record',), build), range(4)))
        self.assertEqual([{'fragment': 'same'}] * 4, results)
        single = FragmentCache()
        single.get(('record',), lambda: {'fragment': 'same'})
        self.assertEqual(single.bytes_used, cache.bytes_used)
