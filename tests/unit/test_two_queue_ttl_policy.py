"""Unit tests for TwoQueueTTLPolicy."""
import pytest

from sim.core.block import BlockQueue
from sim.io.registry import OfflineRegistry
from sim.policies.two_queue_ttl import TwoQueueTTLPolicy


def make_policy(
    capacity: int = 10,
    base_ttl: float = 46.0,
    extended_ttl: float = 255.0,
    promotion_threshold: int = 2,
    protected_ratio: float = 0.30,
    registry: OfflineRegistry = None,
) -> TwoQueueTTLPolicy:
    return TwoQueueTTLPolicy(
        capacity=capacity,
        base_ttl=base_ttl,
        extended_ttl=extended_ttl,
        promotion_threshold=promotion_threshold,
        protected_ratio=protected_ratio,
        registry=registry or OfflineRegistry.empty(),
    )


class TestQueueAssignment:
    def test_new_block_enters_probation(self):
        p = make_policy()
        p.add("h1", "", 0.0, 0, "u1")
        assert p._probation["h1"].queue == BlockQueue.PROBATION

    def test_registry_block_enters_protected(self):
        reg = OfflineRegistry(protected={"h1"})
        p = make_policy(registry=reg)
        p.add("h1", "", 0.0, 0, "u1")
        assert "h1" in p._protected
        assert "h1" not in p._probation

    def test_registry_block_gets_ttl(self):
        reg = OfflineRegistry(protected={"h1"})
        p = make_policy(registry=reg, base_ttl=46.0)
        p.add("h1", "", 100.0, 0, "u1")
        assert p._protected["h1"].ttl_expiry == pytest.approx(146.0)


class TestPromotion:
    def test_promotion_on_second_hit(self):
        p = make_policy(promotion_threshold=2)
        p.add("h1", "", 0.0, 0, "u1")
        p.access("h1", 1.0, 0, "u1")   # hit_count → 1, still in Probation
        assert "h1" in p._probation
        p.access("h1", 2.0, 0, "u1")   # hit_count → 2, triggers promotion
        assert "h1" in p._protected
        assert "h1" not in p._probation

    def test_promoted_block_gets_ttl(self):
        p = make_policy(promotion_threshold=2, base_ttl=46.0)
        p.add("h1", "", 0.0, 0, "u1")
        p.access("h1", 1.0, 0, "u1")
        p.access("h1", 50.0, 0, "u1")  # promotion at t=50
        assert p._protected["h1"].ttl_expiry == pytest.approx(50.0 + 46.0)

    def test_promotion_emits_event(self):
        p = make_policy(promotion_threshold=2)
        p.add("h1", "", 0.0, 0, "u1")
        p.access("h1", 1.0, 0, "u1")
        p.access("h1", 2.0, 0, "u1")
        events = p.flush_events()
        assert "h1" in events.promotions

    def test_flush_clears_events(self):
        p = make_policy(promotion_threshold=2)
        p.add("h1", "", 0.0, 0, "u1")
        p.access("h1", 1.0, 0, "u1")
        p.access("h1", 2.0, 0, "u1")
        p.flush_events()
        events2 = p.flush_events()
        assert events2.promotions == []

    def test_extended_ttl_on_high_hit_count(self):
        p = make_policy(base_ttl=46.0, extended_ttl=255.0)
        p.add("h1", "", 0.0, 0, "u1")
        # Promote first
        for t in range(1, 3):
            p.access("h1", float(t), 0, "u1")
        # Drive hit_count to 5
        for t in range(3, 7):
            p.access("h1", float(t), 0, "u1")  # hit_count goes 3→4→5→6
        # At hit_count >= 5, ttl_expiry should use extended_ttl
        meta = p._protected["h1"]
        assert meta.hit_count >= 5
        assert meta.ttl_expiry == pytest.approx(6.0 + 255.0)


class TestEvictionPriority:
    def test_evicts_probation_before_protected(self):
        # Add one protected block (via registry)
        reg = OfflineRegistry(protected={"p1"})
        p2 = make_policy(capacity=4, protected_ratio=0.5, base_ttl=100.0, registry=reg)
        p2.add("p1", "", 0.0, 0, "u1")
        p2.add("q1", "", 1.0, 0, "u1")   # goes to Probation
        evicted = p2.evict_one(50.0)  # TTL of p1 not yet expired (100s)
        assert evicted.block_key == "q1"

    def test_evicts_ttl_expired_protected_before_valid(self):
        p = make_policy(capacity=10, protected_ratio=0.5, base_ttl=10.0)
        # Manually plant blocks into protected
        p.add("h1", "", 0.0, 0, "u1")
        p.access("h1", 1.0, 0, "u1")
        p.access("h1", 2.0, 0, "u1")  # promoted at t=2, ttl_expiry=2+10=12

        p.add("h2", "", 0.0, 0, "u1")
        p.access("h2", 1.0, 0, "u1")
        p.access("h2", 50.0, 0, "u1")  # promoted at t=50, ttl_expiry=50+10=60

        # At t=30: h1 ttl expired (12 < 30), h2 not expired (60 > 30)
        # Probation is empty → should evict h1 from Protected first
        evicted = p.evict_one(30.0)
        assert evicted.block_key == "h1"

    def test_evict_respects_pinned(self):
        p = make_policy(capacity=4)
        p.add("h1", "", 0.0, 0, "u1")
        p.add("h2", "", 1.0, 0, "u1")
        evicted = p.evict_one(2.0, pinned={"h1"})
        assert evicted.block_key == "h2"


class TestTTLRefresh:
    def test_protected_hit_refreshes_ttl(self):
        p = make_policy(promotion_threshold=2, base_ttl=46.0)
        p.add("h1", "", 0.0, 0, "u1")
        p.access("h1", 1.0, 0, "u1")
        p.access("h1", 2.0, 0, "u1")  # promoted, ttl_expiry = 2 + 46 = 48
        p.access("h1", 100.0, 0, "u1")  # refresh: ttl_expiry = 100 + 46 = 146
        assert p._protected["h1"].ttl_expiry == pytest.approx(146.0)


class TestSize:
    def test_size_tracks_both_queues(self):
        p = make_policy(capacity=10)
        p.add("h1", "", 0.0, 0, "u1")
        p.add("h2", "", 1.0, 0, "u1")
        assert p.size == 2
        p.access("h1", 2.0, 0, "u1")
        p.access("h1", 3.0, 0, "u1")  # promoted
        assert p.size == 2
        assert p.probation_size == 1
        assert p.protected_size == 1


class TestEvictionQueueOrder:
    def test_one_hit_block_stays_in_probation(self):
        """One hit below promotion_threshold=2 must not move block to Protected."""
        p = make_policy(promotion_threshold=2)
        p.add("h1", "", 0.0, 0, "u1")
        p.access("h1", 1.0, 0, "u1")   # hit_count=1, below threshold
        assert "h1" in p._probation
        assert "h1" not in p._protected

    def test_expired_protected_evicted_before_probation(self):
        """TTL-expired Protected block is evicted before a Probation block.

        Rationale: a Protected block with expired TTL is stale (session ended
        > base_ttl ago).  A Probation block may be new session content about to
        be reused.  Stale Protected blocks should clear first.
        """
        reg = OfflineRegistry(protected={"p1"})
        p = make_policy(capacity=4, base_ttl=5.0, registry=reg)
        p.add("p1", "", 0.0, 0, "u1")   # enters Protected immediately; ttl_expiry=5
        p.add("q1", "", 1.0, 0, "u1")   # enters Probation
        # At t=10: p1.ttl=5 expired, q1 is in Probation
        evicted = p.evict_one(10.0)
        assert evicted.block_key == "p1"   # expired Protected evicted before Probation

    def test_protected_expired_evicted_before_valid_protected(self):
        """Among Protected blocks only, expired one is evicted before valid one."""
        reg = OfflineRegistry(protected={"p1", "p2"})
        p = make_policy(capacity=4, base_ttl=10.0, registry=reg)
        p.add("p1", "", 0.0, 0, "u1")    # Protected, ttl_expiry=10
        p.add("p2", "", 50.0, 0, "u1")   # Protected, ttl_expiry=60
        # Probation is empty; at t=20: p1 expired (10<20), p2 valid (60>20)
        evicted = p.evict_one(20.0)
        assert evicted.block_key == "p1"
