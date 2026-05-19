"""Unit tests for LRUPolicy."""

from sim.policies.lru import LRUPolicy


def make_policy(capacity: int = 4) -> LRUPolicy:
    return LRUPolicy(capacity=capacity)


class TestBasicOperations:
    def test_empty_cache_miss(self):
        p = make_policy()
        assert p.access("h1", 0.0, 0, "u1") is False

    def test_add_then_hit(self):
        p = make_policy()
        p.add("h1", "", 0.0, 0, "u1")
        assert p.access("h1", 1.0, 0, "u1") is True

    def test_contains(self):
        p = make_policy()
        assert p.contains("h1") is False
        p.add("h1", "", 0.0, 0, "u1")
        assert p.contains("h1") is True

    def test_hit_increments_count(self):
        p = make_policy()
        p.add("h1", "", 0.0, 0, "u1")
        p.access("h1", 1.0, 0, "u1")
        p.access("h1", 2.0, 0, "u1")
        assert p._cache["h1"].hit_count == 2

    def test_size(self):
        p = make_policy(capacity=4)
        assert p.size == 0
        p.add("h1", "", 0.0, 0, "u1")
        p.add("h2", "", 0.0, 0, "u1")
        assert p.size == 2


class TestEviction:
    def test_evicts_lru_block(self):
        p = make_policy(capacity=2)
        p.add("h1", "", 1.0, 0, "u1")
        p.add("h2", "", 2.0, 0, "u1")
        # h1 is LRU; evict should remove it
        evicted = p.evict_one(3.0)
        assert evicted is not None
        assert evicted.block_key == "h1"
        assert p.contains("h1") is False

    def test_access_refreshes_lru(self):
        p = make_policy(capacity=2)
        p.add("h1", "", 1.0, 0, "u1")
        p.add("h2", "", 2.0, 0, "u1")
        # Access h1 → now h2 is LRU
        p.access("h1", 3.0, 0, "u1")
        evicted = p.evict_one(4.0)
        assert evicted.block_key == "h2"

    def test_evict_respects_pinned(self):
        p = make_policy(capacity=2)
        p.add("h1", "", 1.0, 0, "u1")
        p.add("h2", "", 2.0, 0, "u1")
        evicted = p.evict_one(3.0, pinned={"h1"})
        assert evicted.block_key == "h2"

    def test_evict_returns_none_when_all_pinned(self):
        p = make_policy(capacity=2)
        p.add("h1", "", 1.0, 0, "u1")
        evicted = p.evict_one(2.0, pinned={"h1"})
        assert evicted is None

    def test_is_full(self):
        p = make_policy(capacity=2)
        p.add("h1", "", 1.0, 0, "u1")
        assert p.is_full() is False
        p.add("h2", "", 2.0, 0, "u1")
        assert p.is_full() is True


class TestCapacityAndEvictionLoop:
    def test_eviction_on_overflow(self):
        """Simulates what PrefixCache does: evict until room, then add."""
        p = make_policy(capacity=2)
        p.add("h1", "", 1.0, 0, "u1")
        p.add("h2", "", 2.0, 0, "u1")
        assert p.is_full()
        evicted = p.evict_one(3.0)
        assert evicted is not None
        p.add("h3", "", 3.0, 0, "u1")
        assert p.size == 2
        assert p.contains("h3")
