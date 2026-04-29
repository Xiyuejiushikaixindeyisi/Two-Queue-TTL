"""Integration tests on the 7-row toy trace.

Toy trace (tests/fixtures/toy_trace.csv):
  R1  t=1.0   [A,B,C]       → all miss (3 new blocks)
  R2  t=2.0   [D,E]         → all miss, evict 1 (capacity=4)
  R3  t=3.0   [A,B,C]       → A missed (evicted in R2), 3 misses
  R4  t=4.0   [A,B,C,F]     → A,B,C hit; F miss, evict 1
  R5  t=9.1   [A,B]         → A,B hit (TTL expired but TTL-LRU still returns True)
  R6  t=10.0  [D,E]         → all miss, evict 2
  R7  t=11.0  [A,B,C]       → A,B hit; C miss, evict 1

Hand-calculated expected values for capacity=4, block_size=1:
  total_requests         = 7
  total_blocks_requested = 19  (3+2+3+4+2+2+3)
  total_blocks_hit       = 7   (0+0+0+3+2+0+2)  — same for all three policies
  total_blocks_miss      = 12
  prefix_block_hit_rate  = 7/19 ≈ 0.368421
  saved_prefill_tokens   = 7   (block_size=1)
  eviction_count         = 8   — same for all three policies
  hot_prefix_eviction_count        = 0  (no evicted block had hit_count ≥ 2)
  evicted_before_next_hit_count    = 6
  promotion_count: LRU=0, TTL-LRU=0, TwoQueueTTL=2
  TwoQueueTTL protected_eviction_count = 0  (all evictions from Probation)
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from sim.config import ExperimentConfig, SimConfig, TwoQueueTTLConfig, TTLLRUConfig
from sim.experiment import ExperimentRunner
from sim.io.trace_loader import load_trace
from sim.runner import SimulationRunner

TOY_TRACE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "fixtures",
    "toy_trace.csv",
)

CAPACITY = 4
BLOCK_SIZE = 1
TTL = 5.0
PROMOTION_THRESHOLD = 2


@pytest.fixture(scope="module")
def toy_trace():
    return load_trace(TOY_TRACE)


# ---------------------------------------------------------------------------
# Invariants shared by all three policies
# ---------------------------------------------------------------------------


class TestSharedInvariants:
    @pytest.mark.parametrize("policy", ["lru", "ttl_lru", "two_queue_ttl"])
    def test_total_requests(self, toy_trace, policy):
        cfg = _make_config(policy)
        s = SimulationRunner(cfg).run(toy_trace)
        assert s.total_requests == 7

    @pytest.mark.parametrize("policy", ["lru", "ttl_lru", "two_queue_ttl"])
    def test_total_blocks_requested(self, toy_trace, policy):
        cfg = _make_config(policy)
        s = SimulationRunner(cfg).run(toy_trace)
        assert s.total_blocks_requested == 19

    @pytest.mark.parametrize("policy", ["lru", "ttl_lru", "two_queue_ttl"])
    def test_hit_plus_miss_equals_total(self, toy_trace, policy):
        cfg = _make_config(policy)
        s = SimulationRunner(cfg).run(toy_trace)
        assert s.total_blocks_hit + s.total_blocks_miss == s.total_blocks_requested

    @pytest.mark.parametrize("policy", ["lru", "ttl_lru", "two_queue_ttl"])
    def test_saved_tokens(self, toy_trace, policy):
        cfg = _make_config(policy)
        s = SimulationRunner(cfg).run(toy_trace)
        assert s.saved_prefill_tokens == s.total_blocks_hit * BLOCK_SIZE


# ---------------------------------------------------------------------------
# LRU expected values
# ---------------------------------------------------------------------------


class TestLRUExpected:
    @pytest.fixture(scope="class")
    def snapshot(self, toy_trace):
        return SimulationRunner(_make_config("lru")).run(toy_trace)

    def test_hits(self, snapshot):
        assert snapshot.total_blocks_hit == 7

    def test_misses(self, snapshot):
        assert snapshot.total_blocks_miss == 12

    def test_hit_rate(self, snapshot):
        assert abs(snapshot.prefix_block_hit_rate - 7 / 19) < 1e-6

    def test_eviction_count(self, snapshot):
        assert snapshot.eviction_count == 8

    def test_hot_prefix_eviction(self, snapshot):
        assert snapshot.hot_prefix_eviction_count == 0

    def test_evicted_before_next_hit(self, snapshot):
        assert snapshot.evicted_before_next_hit_count == 6

    def test_promotion_count(self, snapshot):
        assert snapshot.promotion_count == 0


# ---------------------------------------------------------------------------
# TTL-LRU expected values
# TTL semantics: expired block is still a HIT. TTL only affects eviction order.
# At R5 (t=9.1), blocks A and B have ttl_expiry=9.0 (expired), but access() still True.
# This makes TTL-LRU hit count identical to LRU on this trace.
# ---------------------------------------------------------------------------


class TestTTLLRUExpected:
    @pytest.fixture(scope="class")
    def snapshot(self, toy_trace):
        return SimulationRunner(_make_config("ttl_lru")).run(toy_trace)

    def test_hits(self, snapshot):
        assert snapshot.total_blocks_hit == 7

    def test_misses(self, snapshot):
        assert snapshot.total_blocks_miss == 12

    def test_hit_rate(self, snapshot):
        assert abs(snapshot.prefix_block_hit_rate - 7 / 19) < 1e-6

    def test_eviction_count(self, snapshot):
        assert snapshot.eviction_count == 8

    def test_promotion_count(self, snapshot):
        assert snapshot.promotion_count == 0

    def test_r5_expired_blocks_still_hit(self, toy_trace):
        """Regression: TTL-expired blocks at R5 (t=9.1 > ttl_expiry=9.0) must still hit."""
        cfg = _make_config("ttl_lru")
        s = SimulationRunner(cfg).run(toy_trace)
        # R5 is the 5th request; A and B should hit (2 hits) even though TTL expired.
        # Verify by checking overall hit count matches the trace where R5 hits 2 blocks.
        assert s.total_blocks_hit == 7   # would be 5 under old (incorrect) semantics


# ---------------------------------------------------------------------------
# TwoQueueTTL expected values
# ---------------------------------------------------------------------------


class TestTwoQueueTTLExpected:
    @pytest.fixture(scope="class")
    def snapshot(self, toy_trace):
        return SimulationRunner(_make_config("two_queue_ttl")).run(toy_trace)

    def test_hits(self, snapshot):
        assert snapshot.total_blocks_hit == 7

    def test_misses(self, snapshot):
        assert snapshot.total_blocks_miss == 12

    def test_hit_rate(self, snapshot):
        assert abs(snapshot.prefix_block_hit_rate - 7 / 19) < 1e-6

    def test_eviction_count(self, snapshot):
        assert snapshot.eviction_count == 8

    def test_promotion_count(self, snapshot):
        """A and B are promoted after their 2nd hit in R5 (hit_count=2 ≥ threshold=2)."""
        assert snapshot.promotion_count == 2

    def test_no_protected_evictions(self, snapshot):
        """Protected blocks (A and B) are never evicted in this trace."""
        assert snapshot.protected_eviction_count == 0

    def test_pollution_rate_zero(self, snapshot):
        assert snapshot.protected_pollution_rate == 0.0

    def test_hot_prefix_eviction_zero(self, snapshot):
        assert snapshot.hot_prefix_eviction_count == 0

    def test_evicted_before_next_hit(self, snapshot):
        assert snapshot.evicted_before_next_hit_count == 6


# ---------------------------------------------------------------------------
# Multi-policy comparison (shared trace invariant)
# ---------------------------------------------------------------------------


class TestComparison:
    @pytest.fixture(scope="class")
    def snapshots(self, toy_trace):
        configs = [
            _make_config("lru"),
            _make_config("ttl_lru"),
            _make_config("two_queue_ttl"),
        ]
        return SimulationRunner.compare(toy_trace, configs)

    def test_same_blocks_requested_across_policies(self, snapshots):
        """All policies must process the same number of blocks (same trace)."""
        requested = [s.total_blocks_requested for s in snapshots]
        assert len(set(requested)) == 1

    def test_two_queue_ttl_has_most_promotions(self, snapshots):
        promotions = {s.policy_name: s.promotion_count for s in snapshots}
        assert promotions["two_queue_ttl"] > promotions["lru"]
        assert promotions["two_queue_ttl"] > promotions["ttl_lru"]


# ---------------------------------------------------------------------------
# ExperimentRunner end-to-end (writes output files)
# ---------------------------------------------------------------------------


class TestExperimentRunner:
    def test_writes_summary_json(self, toy_trace):
        with tempfile.TemporaryDirectory() as tmpdir:
            exp_cfg = ExperimentConfig.from_dict({
                "name": "test_exp",
                "trace_path": TOY_TRACE,
                "output_dir": tmpdir,
                "policies": [
                    {"policy": "lru", "capacity": 4, "block_size": 1},
                    {"policy": "two_queue_ttl", "capacity": 4, "block_size": 1,
                     "two_queue_ttl": {"promotion_threshold": 2, "base_ttl": 10.0,
                                       "extended_ttl": 100.0}},
                ],
            })
            runner = ExperimentRunner(exp_cfg)
            snapshots = runner.run()

            summary_path = os.path.join(tmpdir, "summary.json")
            assert os.path.exists(summary_path)
            with open(summary_path) as f:
                data = json.load(f)
            assert data["experiment"] == "test_exp"
            assert len(data["results"]) == 2

    def test_writes_comparison_json(self, toy_trace):
        with tempfile.TemporaryDirectory() as tmpdir:
            exp_cfg = ExperimentConfig.from_dict({
                "name": "test_exp",
                "trace_path": TOY_TRACE,
                "output_dir": tmpdir,
                "policies": [
                    {"policy": "lru", "capacity": 4, "block_size": 1},
                    {"policy": "ttl_lru", "capacity": 4, "block_size": 1,
                     "ttl_lru": {"ttl": 5.0}},
                ],
            })
            runner = ExperimentRunner(exp_cfg)
            runner.run()

            cmp_path = os.path.join(tmpdir, "comparison.json")
            assert os.path.exists(cmp_path)
            with open(cmp_path) as f:
                rows = json.load(f)
            metrics = {r["metric"] for r in rows}
            assert "prefix_block_hit_rate" in metrics
            assert "promotion_count" in metrics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(policy: str) -> SimConfig:
    return SimConfig(
        cache_capacity=CAPACITY,
        block_size=BLOCK_SIZE,
        policy=policy,
        ttl_lru=TTLLRUConfig(ttl=TTL),
        two_queue_ttl=TwoQueueTTLConfig(
            promotion_threshold=PROMOTION_THRESHOLD,
            base_ttl=10.0,
            extended_ttl=100.0,
            protected_ratio=0.5,
        ),
    )
