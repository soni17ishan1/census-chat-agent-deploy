"""BoundedCache: the shared eviction logic behind all three in-process
caches (two schema lookups + SQL results). Tested directly here so the
guarantee -- none of them can grow without limit -- isn't just implied by
the higher-level caching tests.
"""
from agent.cache_utils import BoundedCache


def test_get_returns_none_for_missing_key():
    cache = BoundedCache(max_entries=10)
    assert cache.get("missing") is None


def test_put_then_get_round_trips():
    cache = BoundedCache(max_entries=10)
    cache.put("key", {"value": 42})
    assert cache.get("key") == {"value": 42}


def test_evicts_oldest_entry_once_over_the_limit():
    cache = BoundedCache(max_entries=3)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    cache.put("d", 4)  # should evict "a", the oldest

    assert "a" not in cache
    assert cache.get("a") is None
    assert cache.get("d") == 4
    assert len(cache) == 3


def test_get_refreshes_recency_so_it_survives_eviction():
    cache = BoundedCache(max_entries=3)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    cache.get("a")  # touching "a" should make it recently-used again
    cache.put("d", 4)  # now "b" is the oldest untouched entry, not "a"

    assert "a" in cache
    assert "b" not in cache


def test_never_exceeds_max_entries_across_many_inserts():
    cache = BoundedCache(max_entries=5)
    for i in range(100):
        cache.put(i, i)
    assert len(cache) == 5


def test_clear_empties_the_cache():
    cache = BoundedCache(max_entries=10)
    cache.put("a", 1)
    cache.clear()
    assert len(cache) == 0
    assert cache.get("a") is None
