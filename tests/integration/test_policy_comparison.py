"""Integration tests comparing LRU / TTL-LRU / TwoQueueTTL on targeted traces.

Trace B — TwoQueueTTL protects a hot block that LRU/TTL-LRU would evict
--------------------------------------------------------------------
hot block: add (t=0) + hit×2 (t=1, t=2) → promoted to Protected (threshold=2).
Three one-hit-wonder blocks then fill the cache.
At t=6 a new block requires eviction:
  LRU: hot is LRU head (last_access=t=2, newer blocks arrived after) → evict hot
  TwoQueueTTL: hot is Protected → evict Probation LRU head instead
At t=7 [hot] is re-requested: TwoQ hit, LRU miss.

Trace C — TwoQueueTTL anti-pollution (corrected per D4 in round2_plan.md)
--------------------------------------------------------------------------
  R1: [sys, usr1]          sys enters Probation
  R2: [sys, usr2]          sys: 1st hit → promoted to Protected (threshold=1)
  R3..Rn: [new_i]          one-hit-wonder flood; evicts Probation blocks
  R_last: [sys, usr3]      TwoQ: sys still in Protected → hit; LRU: evicted → miss

TTL-expiry-does-not-cause-miss (D5 in round2_plan.md)
------------------------------------------------------
  Capacity large enough to prevent any eviction.
  Block added at t=0 with short TTL, re-requested at t>>TTL.
  Both TTL-LRU and TwoQueueTTL must return a hit.
"""
from __future__ import annotations

import pytest

from sim.cache.prefix_cache import PrefixCache
from sim.config import SimConfig, TwoQueueTTLConfig
from sim.core.trace import TraceRecord
from sim.policies.lru import LRUPolicy
from sim.policies.ttl_lru import TTLLRUPolicy
from sim.policies.two_queue_ttl import TwoQueueTTLPolicy
from sim.runner import SimulationRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def rec(ts: float, hash_ids: list[str], model: str = "m") -> TraceRecord:
    return TraceRecord(
        timestamp=ts,
        model_id=model,
        user_id="u",
        request_type="prefill",
        input_length=len(hash_ids) * 128,
        hash_ids=hash_ids,
    )


def run_with_policy(policy, trace: list[TraceRecord]):
    """Run a trace directly with a policy instance, return RequestResult list."""
    cache = PrefixCache(policy, block_size=128)
    return [cache.process_request(r) for r in trace]


def hit_rate(results) -> float:
    hits = sum(r.hit_count for r in results)
    total = sum(len(r.record.hash_ids) for r in results)
    return hits / total if total else 0.0


def run_snapshot(policy_name: str, trace: list[TraceRecord], capacity: int,
                 two_queue_ttl: TwoQueueTTLConfig | None = None):
    """Run via SimulationRunner with proper SimConfig structure."""
    config = SimConfig(
        cache_capacity=capacity,
        policy=policy_name,
        block_size=128,
        two_queue_ttl=two_queue_ttl or TwoQueueTTLConfig(),
    )
    return SimulationRunner(config).run(trace)


# ---------------------------------------------------------------------------
# Trace B: TwoQueueTTL protects hot block; LRU evicts it under pressure
# ---------------------------------------------------------------------------

def _make_trace_b(capacity: int = 4) -> list[TraceRecord]:
    """
    Warmup (3 requests with [hot, ...]):
      t=0: hot miss (add), u0 miss (add)
      t=1: hot hit #1 (hit_count=1), u1 miss (add)
      t=2: hot hit #2 (hit_count=2 → TwoQ: promoted to Protected), u2 miss (add)
    After t=2: cache = {hot, u0, u1, u2}, full (capacity=4).
    LRU order at this point: [u0(t=0), u1(t=1), hot(t=2 hit), u2(t=2 add)]
    i.e. u0 is LRU head, u2 is MRU.

    Pollution (3 requests evict u0, u1, u2):
      t=3: evict u0 (LRU head / Probation LRU); add new0
      t=4: evict u1; add new1
      t=5: evict u2; add new2

    After t=5:
      TwoQ: Protected={hot}, Probation={new0, new1, new2}
      LRU:  cache={hot(t=2), new0(t=3), new1(t=4), new2(t=5)} → hot is LRU head

    Divergence at t=6 [new3]:
      TwoQ: evict new0 (Probation LRU head) → hot stays
      LRU:  evict hot (LRU head, last_access=t=2)

    Verify at t=7 [hot]:
      TwoQ: Protected hit
      LRU:  miss (hot evicted)
    """
    return [
        rec(0.0, ["hot", "u0"]),
        rec(1.0, ["hot", "u1"]),
        rec(2.0, ["hot", "u2"]),
        rec(3.0, ["new0"]),
        rec(4.0, ["new1"]),
        rec(5.0, ["new2"]),
        rec(6.0, ["new3"]),   # divergence: TwoQ keeps hot, LRU evicts hot
        rec(7.0, ["hot"]),    # TwoQ: hit; LRU: miss
    ]


class TestTraceB:
    def test_two_queue_keeps_hot_block_under_pollution(self):
        """TwoQueueTTL's hot block (Protected) survives one-hit-wonder pressure."""
        trace = _make_trace_b(capacity=4)
        results = run_with_policy(
            TwoQueueTTLPolicy(capacity=4, base_ttl=10000.0, promotion_threshold=2),
            trace,
        )
        assert results[-1].hit_count >= 1, (
            "TwoQueueTTL: hot block must remain in Protected after pollution"
        )

    def test_lru_evicts_hot_block_under_pollution(self):
        """LRU evicts hot when it becomes LRU head after pollution fills cache."""
        trace = _make_trace_b(capacity=4)
        results = run_with_policy(LRUPolicy(capacity=4), trace)
        assert results[-1].hit_count == 0, (
            "LRU: hot block should be evicted (it is the LRU head at t=6)"
        )

    def test_ttl_lru_behaves_like_lru_with_no_expiry_on_trace_b(self):
        """TTL-LRU with TTL >> trace duration behaves identically to LRU."""
        trace = _make_trace_b(capacity=4)
        results = run_with_policy(
            TTLLRUPolicy(capacity=4, ttl=100_000.0),
            trace,
        )
        # No TTL expiry occurs; eviction is pure LRU → same as LRU test
        assert results[-1].hit_count == 0, (
            "TTL-LRU (no expiry): should behave like LRU and evict hot"
        )

    def test_two_queue_higher_hit_rate_than_lru_on_trace_b(self):
        """TwoQueueTTL achieves strictly higher prefix hit rate than LRU on Trace B."""
        trace = _make_trace_b(capacity=4)
        tq_snap = run_snapshot(
            "two_queue_ttl", trace, capacity=4,
            two_queue_ttl=TwoQueueTTLConfig(base_ttl=10000.0, promotion_threshold=2),
        )
        lru_snap = run_snapshot("lru", trace, capacity=4)
        assert tq_snap.prefix_block_hit_rate > lru_snap.prefix_block_hit_rate, (
            f"TwoQ {tq_snap.prefix_block_hit_rate:.3f} should exceed "
            f"LRU {lru_snap.prefix_block_hit_rate:.3f}"
        )


# ---------------------------------------------------------------------------
# Trace C (aggressive_promotion): promotion_threshold=1
#
# Scenario: sys appears twice before pollution.
#   R1: [sys, usr1]  →  sys miss (add, hit_count=0)
#   R2: [sys, usr2]  →  sys hit #1 (hit_count=1 >= threshold=1 → PROMOTED)
#   R3+: [new_i]     →  one-hit-wonder flood
#   R_last: [sys, usr3]
#
# "Aggressive" because a single post-insertion hit triggers promotion.
# Matches the user scenario: "R2 = sys 第二次出现，第一次命中，即晋升".
#
# promotion_threshold semantics reminder (see docs/terminology.md §7):
#   hit_count counts only cache HITs, NOT the initial add().
#   threshold=1 → 2 trace appearances (1 miss + 1 hit) to reach Protected.
#   threshold=2 → 3 trace appearances (1 miss + 2 hits) to reach Protected.  ← default
# ---------------------------------------------------------------------------

def _make_trace_c_aggressive(n_pollution: int = 8, capacity: int = 4) -> list[TraceRecord]:
    """
    R1 (t=0): [sys, usr1]    sys: miss (Probation), usr1: miss
    R2 (t=1): [sys, usr2]    sys: hit #1 → PROMOTED (threshold=1); usr2: miss
    After R2: Protected={sys}, Probation={usr1_key, usr2_key}; total=3 blocks
    R3 (t=2): [new_0]        total=4, FULL; no eviction needed
    R4+ (t=3+): [new_i]      evict Probation LRU; sys stays in Protected
    R_last:   [sys, usr3]    TwoQ: Protected hit; LRU: miss (sys evicted ~R5)
    """
    trace = [
        rec(0.0, ["sys", "usr1"]),
        rec(1.0, ["sys", "usr2"]),
    ]
    for i in range(n_pollution):
        trace.append(rec(2.0 + i, [f"new_{i}"]))
    trace.append(rec(2.0 + n_pollution, ["sys", "usr3"]))
    return trace


class TestTraceCAggressivePromotion:
    """Trace C with promotion_threshold=1 (aggressive): sys promoted after 1st hit."""

    def test_two_queue_sys_survives_pollution_aggressive_promotion(self):
        """sys block (Protected after 1st hit) survives one-hit-wonder flood."""
        trace = _make_trace_c_aggressive(n_pollution=8, capacity=4)
        results = run_with_policy(
            TwoQueueTTLPolicy(capacity=4, base_ttl=10000.0, promotion_threshold=1),
            trace,
        )
        last = results[-1]
        assert last.hit_count >= 1, (
            "TwoQueueTTL (threshold=1): sys block must stay in Protected through pollution"
        )

    def test_lru_sys_evicted_by_pollution_aggressive(self):
        """LRU evicts sys (the LRU head) during early pollution requests."""
        trace = _make_trace_c_aggressive(n_pollution=8, capacity=4)
        results = run_with_policy(LRUPolicy(capacity=4), trace)
        last = results[-1]
        assert last.hit_count == 0, (
            "LRU: sys is LRU head (last_access=t=1) and gets evicted early in pollution"
        )

    def test_two_queue_higher_hit_rate_than_lru_aggressive(self):
        """TwoQueueTTL (threshold=1) achieves strictly higher hit rate than LRU."""
        trace = _make_trace_c_aggressive(n_pollution=8, capacity=4)
        tq_snap = run_snapshot(
            "two_queue_ttl", trace, capacity=4,
            two_queue_ttl=TwoQueueTTLConfig(base_ttl=10000.0, promotion_threshold=1),
        )
        lru_snap = run_snapshot("lru", trace, capacity=4)
        assert tq_snap.prefix_block_hit_rate > lru_snap.prefix_block_hit_rate


# ---------------------------------------------------------------------------
# Trace C (default_promotion): promotion_threshold=2 (default config)
#
# Scenario: sys appears three times before pollution.
#   R1: [sys, u1]  →  sys miss (hit_count=0)
#   R2: [sys, u2]  →  sys hit #1 (hit_count=1, still Probation)
#   R3: [sys, u3]  →  sys hit #2 (hit_count=2 >= threshold=2 → PROMOTED)
#   R4+: [new_i]   →  one-hit-wonder flood
#   R_last: [sys, u4]
#
# LRU eviction trace (capacity=4):
#   After R3: LRU order = [u1(t=0), u2(t=1), sys(t=2 hit), u3(t=2 add)]
#   R4: evict u1; R5: evict u2; R6: evict sys (sys is now LRU head!) → sys gone
#   TwoQueueTTL: sys in Protected → never evicted during pollution
# ---------------------------------------------------------------------------

def _make_trace_c_default(n_pollution: int = 6, capacity: int = 4) -> list[TraceRecord]:
    """
    R1 (t=0): [sys, u1]   sys: miss
    R2 (t=1): [sys, u2]   sys: hit #1 (hit_count=1, Probation)
    R3 (t=2): [sys, u3]   sys: hit #2 → PROMOTED (threshold=2, default); u3 added
    After R3: Protected={sys}, Probation={u1,u2,u3}; cache FULL (capacity=4)
    R4 (t=3): [new_0]     evict: TwoQ→u1(Prob LRU), LRU→u1
    R5 (t=4): [new_1]     evict: TwoQ→u2(Prob LRU), LRU→u2
    R6 (t=5): [new_2]     evict: TwoQ→u3(Prob LRU), LRU→sys (sys is now LRU head!)
    R7 (t=6): [new_3]     evict: TwoQ→new_0, LRU→new_0 (sys already gone)
    ...
    R_last: [sys, u4]     TwoQ: Protected hit; LRU: miss (sys evicted at R6)
    """
    trace = [
        rec(0.0, ["sys", "u1"]),
        rec(1.0, ["sys", "u2"]),
        rec(2.0, ["sys", "u3"]),
    ]
    for i in range(n_pollution):
        trace.append(rec(3.0 + i, [f"new_{i}"]))
    trace.append(rec(3.0 + n_pollution, ["sys", "u4"]))
    return trace


class TestTraceCDefaultPromotion:
    """Trace C with default promotion_threshold=2: sys promoted after 2nd hit (3rd appearance)."""

    def test_two_queue_sys_survives_pollution_default_promotion(self):
        """sys block (Protected after 2nd hit) survives one-hit-wonder flood."""
        trace = _make_trace_c_default(n_pollution=6, capacity=4)
        results = run_with_policy(
            TwoQueueTTLPolicy(capacity=4, base_ttl=10000.0, promotion_threshold=2),
            trace,
        )
        last = results[-1]
        assert last.hit_count >= 1, (
            "TwoQueueTTL (threshold=2, default): sys must be in Protected at R_last"
        )

    def test_lru_sys_evicted_by_pollution_default(self):
        """LRU evicts sys when it becomes LRU head after u1/u2 are evicted."""
        trace = _make_trace_c_default(n_pollution=6, capacity=4)
        results = run_with_policy(LRUPolicy(capacity=4), trace)
        last = results[-1]
        # After R3: LRU order = [u1, u2, sys, u3]; u1 evicted at R4, u2 at R5,
        # then sys is LRU head and evicted at R6.
        assert last.hit_count == 0, (
            "LRU: sys becomes LRU head after pollution evicts u1, u2; evicted at R6"
        )

    def test_two_queue_higher_hit_rate_than_lru_default_promotion(self):
        """TwoQueueTTL (default threshold=2) achieves higher hit rate than LRU."""
        trace = _make_trace_c_default(n_pollution=6, capacity=4)
        tq_snap = run_snapshot(
            "two_queue_ttl", trace, capacity=4,
            two_queue_ttl=TwoQueueTTLConfig(base_ttl=10000.0, promotion_threshold=2),
        )
        lru_snap = run_snapshot("lru", trace, capacity=4)
        assert tq_snap.prefix_block_hit_rate > lru_snap.prefix_block_hit_rate, (
            f"TwoQ (default) {tq_snap.prefix_block_hit_rate:.3f} should exceed "
            f"LRU {lru_snap.prefix_block_hit_rate:.3f}"
        )


# ---------------------------------------------------------------------------
# TTL expiry must never cause a miss in PrefixCache (D5)
# ---------------------------------------------------------------------------

class TestTTLExpiryDoesNotCauseMiss:
    def test_ttl_expiration_does_not_cause_miss_ttl_lru(self):
        """
        TTL-LRU: block added at t=0 with TTL=1s, re-requested at t=100.
        Capacity=100 prevents eviction.  Must be a HIT (not a miss).
        """
        trace = [
            rec(0.0, ["block_a"]),    # add; ttl_expiry=1.0
            rec(100.0, ["block_a"]),  # TTL expired (1 << 100), still in cache
        ]
        results = run_with_policy(TTLLRUPolicy(capacity=100, ttl=1.0), trace)
        assert results[0].hit_count == 0, "first request must be a cold miss"
        assert results[1].hit_count == 1, "second request must hit despite TTL expiry"

    def test_ttl_expiration_does_not_cause_miss_two_queue(self):
        """
        TwoQueueTTL: block reaches Protected, TTL expires, block is re-requested.
        Capacity=100 prevents eviction.  Must still be a HIT.
        """
        trace = [
            rec(0.0, ["block_a"]),    # add → Probation
            rec(1.0, ["block_a"]),    # hit #1, threshold=1 → promoted to Protected; ttl=2.0
            rec(100.0, ["block_a"]),  # ttl_expiry=2 << 100, but still in Protected → HIT
        ]
        results = run_with_policy(
            TwoQueueTTLPolicy(capacity=100, base_ttl=1.0, promotion_threshold=1),
            trace,
        )
        assert results[0].hit_count == 0
        assert results[1].hit_count == 1
        assert results[2].hit_count == 1, "Protected block must hit despite TTL expiry"

    def test_ttl_expiry_both_policies_agree_on_hit(self):
        """
        Both TTL-LRU and TwoQueueTTL must produce a hit after TTL expiry.
        Verifies the D1 contract holds end-to-end through PrefixCache.
        """
        trace = [
            rec(0.0, ["x"]),
            rec(0.5, ["x"]),    # hit #1; TwoQ: promoted (threshold=1)
            rec(200.0, ["x"]),  # TTL expired in both policies; still in cache
        ]
        ttl_results = run_with_policy(TTLLRUPolicy(capacity=100, ttl=1.0), trace)
        tq_results = run_with_policy(
            TwoQueueTTLPolicy(capacity=100, base_ttl=1.0, promotion_threshold=1),
            trace,
        )
        assert ttl_results[-1].hit_count == 1, "TTL-LRU: expired block must still hit"
        assert tq_results[-1].hit_count == 1, "TwoQueueTTL: expired Protected block must still hit"
