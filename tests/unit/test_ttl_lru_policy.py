"""Unit tests for TTLLRUPolicy.

Key semantic contract (D1/D2 in round2_plan.md):
  - TTL expiry does NOT cause a lookup miss.
  - access() returns True for any block still in cache, regardless of TTL state.
  - TTL only affects evict_one() victim selection priority:
      first: TTL-expired blocks (LRU order among them)
      then:  non-expired blocks (LRU order, fallback)
  - ttl_expiry is refreshed on every hit (including when already expired).
"""
import heapq

import pytest

from sim.policies.ttl_lru import TTLLRUPolicy


def make_policy(capacity: int = 4, ttl: float = 10.0) -> TTLLRUPolicy:
    return TTLLRUPolicy(capacity=capacity, ttl=ttl)


# ---------------------------------------------------------------------------
# TTL does NOT cause lookup miss (D1)
# ---------------------------------------------------------------------------


class TestTTLDoesNotCauseMiss:
    def test_expired_block_still_a_hit(self):
        """access() returns True even after TTL has expired."""
        p = make_policy(ttl=5.0)
        p.add("h1", "c1", 0.0, 0, "u1")      # h1.ttl_expiry = 5.0
        result = p.access("h1", 10.0, 0, "u1")  # t=10 >> ttl_expiry=5
        assert result is True

    def test_expired_block_increments_hit_count(self):
        """hit_count still increases on an expired block access."""
        p = make_policy(ttl=5.0)
        p.add("h1", "c1", 0.0, 0, "u1")
        p.access("h1", 10.0, 0, "u1")
        assert p._cache["h1"].hit_count == 1

    def test_ttl_refreshed_on_hit_even_when_expired(self):
        """ttl_expiry is reset to access_time + ttl, even if already expired."""
        p = make_policy(ttl=5.0)
        p.add("h1", "c1", 0.0, 0, "u1")         # ttl_expiry = 5.0
        p.access("h1", 20.0, 0, "u1")            # refresh: ttl_expiry = 25.0
        assert p._cache["h1"].ttl_expiry == pytest.approx(25.0)

    def test_absent_block_still_miss(self):
        """A block not in cache is still a miss (unrelated to TTL)."""
        p = make_policy(ttl=5.0)
        assert p.access("nonexistent", 0.0, 0, "u1") is False


# ---------------------------------------------------------------------------
# add() sets ttl_expiry correctly
# ---------------------------------------------------------------------------


class TestAddSetsTTL:
    def test_add_sets_ttl_expiry(self):
        p = make_policy(ttl=10.0)
        p.add("h1", "c1", 100.0, 0, "u1")
        assert p._cache["h1"].ttl_expiry == pytest.approx(110.0)

    def test_add_two_blocks_different_ttl_expiry(self):
        p = make_policy(ttl=10.0)
        p.add("h1", "c1", 0.0, 0, "u1")
        p.add("h2", "c2", 5.0, 0, "u1")
        assert p._cache["h1"].ttl_expiry == pytest.approx(10.0)
        assert p._cache["h2"].ttl_expiry == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# evict_one() prefers TTL-expired blocks (D2)
# ---------------------------------------------------------------------------


class TestEvictionPriority:
    def test_expired_block_evicted_before_non_expired(self):
        """evict_one() picks TTL-expired block first, even if LRU says otherwise."""
        p = make_policy(capacity=2, ttl=10.0)
        p.add("early", "c1", 0.0, 0, "u1")    # ttl_expiry=10; early entry → LRU head
        p.add("late", "c2", 5.0, 0, "u1")     # ttl_expiry=15; late entry → LRU tail
        # At t=12: early is expired (10<12), late is not (15>12)
        # LRU would pick "early" (LRU head) — happens to agree with TTL here.
        evicted = p.evict_one(12.0)
        assert evicted.block_key == "early"

    def test_expired_evicted_even_when_lru_order_disagrees(self):
        """
        "recent" was added last (LRU tail → LRU would NOT evict it),
        but its TTL expires first (ttl_expiry is smaller).
        "old" was added first (LRU head → LRU wants to evict it),
        but its TTL expires later.

        Setup: access "old" after adding "recent" to make "old" the MRU
        and "recent" the LRU head.  Give "recent" a short TTL.

        This forces LRU order ≠ TTL expiry order, proving TTL wins.
        """
        p = make_policy(capacity=2, ttl=100.0)  # default long TTL
        p.add("short_lived", "c1", 0.0, 0, "u1")   # ttl_expiry=100
        p.add("long_lived", "c2", 1.0, 0, "u1")    # ttl_expiry=101
        # Manually shorten short_lived's TTL to force expiry before long_lived
        p._cache["short_lived"].ttl_expiry = 5.0
        # Access long_lived → long_lived becomes MRU, short_lived is LRU head
        p.access("long_lived", 2.0, 0, "u1")
        # LRU order: [short_lived(added=0), long_lived(last=2)]
        # At t=10: short_lived expired (5<10); long_lived not expired (101>10)
        # TTL-LRU evicts short_lived (expired first)
        # LRU would ALSO evict short_lived (LRU head) — agrees here.
        # To truly show disagreement, use capacity=3:
        p3 = make_policy(capacity=3, ttl=100.0)
        p3.add("A", "c1", 0.0, 0, "u1")   # A: LRU pos 0, ttl=100
        p3.add("B", "c2", 1.0, 0, "u1")   # B: LRU pos 1, ttl=101
        p3.add("C", "c3", 2.0, 0, "u1")   # C: LRU pos 2, ttl=102
        # Force C to expire earliest — direct mutation of internal state.
        # Must also push a fresh heap entry so evict_one() can find it.
        p3._cache["C"].ttl_expiry = 5.0
        p3._min_expiry = min(p3._min_expiry, 5.0)
        heapq.heappush(p3._expiry_heap, (5.0, "C"))
        # LRU order: [A(0), B(1), C(2)] — LRU would evict A
        # TTL expiry order: C(5) < A(100) < B(101) — TTL-LRU evicts C
        evicted = p3.evict_one(10.0)
        assert evicted.block_key == "C"   # TTL-expired, not LRU head

    def test_non_expired_evicted_when_no_expired_available(self):
        """Falls back to LRU when no TTL-expired block exists."""
        p = make_policy(capacity=2, ttl=100.0)
        p.add("h1", "c1", 0.0, 0, "u1")   # LRU head
        p.add("h2", "c2", 1.0, 0, "u1")   # LRU tail
        # At t=5: neither expired (100 >> 5)
        evicted = p.evict_one(5.0)
        assert evicted.block_key == "h1"   # LRU fallback

    def test_evict_respects_pinned(self):
        """Pinned blocks are skipped, even if TTL-expired."""
        p = make_policy(capacity=2, ttl=1.0)
        p.add("h1", "c1", 0.0, 0, "u1")   # h1.ttl=1, expired at t=5
        p.add("h2", "c2", 0.0, 0, "u1")   # h2.ttl=1, expired at t=5
        evicted = p.evict_one(5.0, pinned={"h1"})
        assert evicted is not None
        assert evicted.block_key == "h2"   # h1 pinned, h2 evicted

    def test_evict_returns_none_when_all_pinned(self):
        p = make_policy(capacity=1, ttl=1.0)
        p.add("h1", "c1", 0.0, 0, "u1")
        evicted = p.evict_one(5.0, pinned={"h1"})
        assert evicted is None
