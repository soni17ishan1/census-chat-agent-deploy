"""A tiny bounded, FIFO-eviction in-memory cache, shared by every in-process
cache in this app (schema lookups and SQL results) so none of them can grow
without limit, and so the eviction behavior is identical everywhere instead
of three different hand-rolled copies of the same logic.
"""
import threading
from collections import OrderedDict
from typing import Generic, Hashable, Optional, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")

DEFAULT_MAX_ENTRIES = 256


class BoundedCache(Generic[K, V]):
    """Example:
        cache = BoundedCache(max_entries=3)
        cache.put("population of California", 39_346_023)
        cache.put("population of Texas", 28_635_442)
        cache.put("population of Florida", 21_216_924)
        cache.put("population of Ohio", 11_675_275)  # cache full -> evicts "California" (oldest)
        cache.get("population of California")  # -> None, already evicted
        cache.get("population of Ohio")  # -> 11_675_275
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES):
        self.max_entries = max_entries
        self._data: "OrderedDict[K, V]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: K) -> Optional[V]:
        with self._lock:
            if key in self._data:
                # Touching an entry marks it "recently used" so it isn't the
                # next thing evicted -- this is what makes eviction order
                # "least recently used", not just "insertion order".
                self._data.move_to_end(key)
                return self._data[key]
        return None

    def put(self, key: K, value: V) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            # popitem(last=False) removes from the *front* of the ordered
            # dict, i.e. the oldest/least-recently-touched entry -- this is
            # the actual eviction. Usually runs 0-1 times per put().
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def __contains__(self, key: K) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
