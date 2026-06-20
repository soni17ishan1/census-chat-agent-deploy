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
    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES):
        self.max_entries = max_entries
        self._data: "OrderedDict[K, V]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: K) -> Optional[V]:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
        return None

    def put(self, key: K, value: V) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
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
