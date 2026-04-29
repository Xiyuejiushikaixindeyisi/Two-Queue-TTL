"""Unit tests for MetricsCollector and MetricsSnapshot."""
import pytest

from sim.cache.prefix_cache import PrefixCache
from sim.config import SimConfig
from sim.core.trace import TraceRecord
from sim.metrics.collector import MetricsCollector, MetricsSnapshot
from sim.policies.lru import LRUPolicy
from sim.runner import SimulationRunner


def make_trace():
    return [
        TraceRecord(1000.0, "m", "u1", "chat", 256, ["sys_a", "sys_b", "usr_1a", "usr_1b"]),
        TraceRecord(1001.0, "m", "u1", "chat", 384, ["sys_a", "sys_b", "usr_1c", "usr_1d"]),
        TraceRecord(1010.0, "m", "u1", "chat", 256, ["sys_a", "sys_b", "usr_1a", "usr_1b"]),
    ]


class TestCollectorCounters:
    def test_total_requests(self):
        trace = make_trace()
        config = SimConfig(cache_capacity=20, policy="lru")
        snapshot = SimulationRunner(config).run(trace)
        assert snapshot.total_requests == 3

    def test_total_blocks_requested(self):
        trace = make_trace()
        config = SimConfig(cache_capacity=20, policy="lru")
        snapshot = SimulationRunner(config).run(trace)
        # 4 + 4 + 4 = 12 blocks
        assert snapshot.total_blocks_requested == 12

    def test_hit_rate_cold_cache(self):
        trace = [make_trace()[0]]  # single request, cold cache
        config = SimConfig(cache_capacity=20, policy="lru")
        snapshot = SimulationRunner(config).run(trace)
        assert snapshot.prefix_block_hit_rate == pytest.approx(0.0)

    def test_hit_rate_after_warmup(self):
        """Third request repeats first → should have full prefix hit."""
        trace = make_trace()
        config = SimConfig(cache_capacity=20, policy="lru")
        snapshot = SimulationRunner(config).run(trace)
        assert snapshot.total_blocks_hit > 0
        assert snapshot.prefix_block_hit_rate > 0.0

    def test_saved_prefill_tokens_proportional_to_hits(self):
        trace = make_trace()
        config = SimConfig(cache_capacity=20, policy="lru", block_size=128)
        snapshot = SimulationRunner(config).run(trace)
        assert snapshot.saved_prefill_tokens == snapshot.total_blocks_hit * 128


class TestFutureAccessPrecomputation:
    def test_evicted_before_next_hit_nonzero(self):
        """With tiny cache, some blocks will be evicted but re-requested later."""
        trace = [
            TraceRecord(1.0, "m", "u1", "c", 128, ["h1", "h2"]),
            TraceRecord(2.0, "m", "u1", "c", 256, ["h3", "h4", "h5"]),  # evicts h1, h2
            TraceRecord(3.0, "m", "u1", "c", 128, ["h1", "h2"]),         # h1, h2 needed again
        ]
        config = SimConfig(cache_capacity=3, policy="lru")
        snapshot = SimulationRunner(config).run(trace)
        # h1 or h2 were evicted at step 2 but needed at step 3
        assert snapshot.evicted_before_next_hit_count > 0


class TestSnapshotProperties:
    def test_hit_rate_zero_division(self):
        snap = MetricsSnapshot(
            policy_name="test",
            cache_capacity=10,
            block_size=128,
            total_blocks_requested=0,
        )
        assert snap.prefix_block_hit_rate == 0.0

    def test_pollution_rate_zero_division(self):
        snap = MetricsSnapshot(
            policy_name="test",
            cache_capacity=10,
            block_size=128,
            protected_eviction_count=0,
        )
        assert snap.protected_pollution_rate == 0.0

    def test_to_dict_keys(self):
        snap = MetricsSnapshot(policy_name="lru", cache_capacity=100, block_size=128)
        d = snap.to_dict()
        required = {
            "policy", "prefix_block_hit_rate", "saved_prefill_tokens",
            "eviction_count", "hot_prefix_eviction_count",
            "evicted_before_next_hit_count", "protected_pollution_rate",
        }
        assert required.issubset(d.keys())


class TestTwoQueueVsLRU:
    def test_two_queue_hit_rate_ge_lru(self):
        """On a trace with repeated prefixes, Two-Queue should not be worse than LRU."""
        trace = [
            TraceRecord(t, "m", "u1", "c", 256, ["sys_a", "sys_b", "usr"])
            for t in range(1, 20)
        ]
        lru_snap = SimulationRunner(SimConfig(cache_capacity=5, policy="lru")).run(trace)
        tq_snap = SimulationRunner(
            SimConfig(cache_capacity=5, policy="two_queue_ttl")
        ).run(trace)
        assert tq_snap.prefix_block_hit_rate >= lru_snap.prefix_block_hit_rate - 0.01
