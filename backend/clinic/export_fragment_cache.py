"""Bounded, process-local memoization of pure export serialization."""
import sys
from collections import OrderedDict
from threading import Lock


def _size(value):
    return sys.getsizeof(value) + (sum(_size(item) for item in value) if isinstance(value, tuple) else 0)


class FragmentCache:
    def __init__(self, limit=64 * 1024 * 1024):
        self.limit = limit
        self.bytes_used = 0
        self._entries = OrderedDict()
        self._lock = Lock()

    def get(self, key, build):
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return dict(cached[0])
        value = build()
        # Values are flat scalar mappings. Charge keys, values and node
        # overhead conservatively; this is an object budget, not an RSS limit.
        size = _size(key) + sys.getsizeof(value) + sum(
            _size(k) + _size(v) for k, v in value.items()) + 256
        if size > self.limit:
            return value
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return dict(cached[0])
            while self.bytes_used + size > self.limit:
                _, (_, removed_size) = self._entries.popitem(last=False)
                self.bytes_used -= removed_size
            self._entries[key] = (dict(value), size)
            self.bytes_used += size
        return value
