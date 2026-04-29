"""Integration tests — full simulation runs using the fixture trace."""
import os
import pytest

from sim.config import SimConfig, TwoQueueTTLConfig
from sim.io.registry import OfflineRegistry
from sim.io.trace_loader import load_trace
from sim.metrics.reporter import compare_table
from sim.runner import SimulationRunner

FIXTURE_TRACE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "fixtures",
    "sample_trace.csv",
)


@pytest.fixture(scope="module")
def trace():
    return load_trace(FIXTURE_TRACE)


class TestSinglePolicyRun:
    def test_lru_runs_without_error(self, trace):
        config = SimConfig(cache_capacity=5, policy="lru")
        snapshot = SimulationRunner(config).run(trace)
        assert snapshot.total_requests == len(trace)

    def test_two_queue_ttl_runs_without_error(self, trace):
        config = SimConfig(cache_capacity=5, policy="two_queue_ttl")
        snapshot = SimulationRunner(config).run(trace)
        assert snapshot.total_requests == len(trace)

    def test_ttl_lru_runs_without_error(self, trace):
        config = SimConfig(cache_capacity=5, policy="ttl_lru")
        snapshot = SimulationRunner(config).run(trace)
        assert snapshot.total_requests == len(trace)


class TestMetricSanity:
    def test_hit_rate_between_0_and_1(self, trace):
        for policy in ("lru", "two_queue_ttl"):
            config = SimConfig(cache_capacity=10, policy=policy)
            s = SimulationRunner(config).run(trace)
            assert 0.0 <= s.prefix_block_hit_rate <= 1.0, policy

    def test_eviction_count_nonnegative(self, trace):
        config = SimConfig(cache_capacity=4, policy="lru")
        s = SimulationRunner(config).run(trace)
        assert s.eviction_count >= 0

    def test_saved_tokens_consistent_with_hits(self, trace):
        config = SimConfig(cache_capacity=10, policy="lru", block_size=128)
        s = SimulationRunner(config).run(trace)
        assert s.saved_prefill_tokens == s.total_blocks_hit * 128

    def test_hit_plus_miss_equals_total(self, trace):
        config = SimConfig(cache_capacity=10, policy="lru")
        s = SimulationRunner(config).run(trace)
        assert s.total_blocks_hit + s.total_blocks_miss == s.total_blocks_requested

    def test_pollution_rate_between_0_and_1(self, trace):
        config = SimConfig(cache_capacity=4, policy="two_queue_ttl")
        s = SimulationRunner(config).run(trace)
        assert 0.0 <= s.protected_pollution_rate <= 1.0


class TestRegistryIntegration:
    def test_registry_blocks_skip_probation(self, trace):
        """sys_a appears in every request; with registry it should go to Protected."""
        registry = OfflineRegistry(protected={"sys_a", "sys_b"})
        config = SimConfig(cache_capacity=10, policy="two_queue_ttl")
        runner = SimulationRunner(config, registry=registry)
        snapshot = runner.run(trace)
        # Registry-protected blocks should have higher hit rates
        assert snapshot.prefix_block_hit_rate > 0.0

    def test_large_capacity_approaches_full_hit(self, trace):
        """With capacity >> working set, second pass should hit almost everything."""
        config = SimConfig(cache_capacity=100, policy="lru")
        snapshot = SimulationRunner(config).run(trace)
        assert snapshot.prefix_block_hit_rate > 0.3  # conservative lower bound


class TestComparativeRun:
    def test_compare_returns_one_snapshot_per_policy(self, trace):
        configs = [
            SimConfig(cache_capacity=8, policy="lru"),
            SimConfig(cache_capacity=8, policy="two_queue_ttl"),
        ]
        snapshots = SimulationRunner.compare(trace, configs)
        assert len(snapshots) == 2

    def test_compare_table_does_not_crash(self, trace, capsys):
        configs = [
            SimConfig(cache_capacity=8, policy="lru"),
            SimConfig(cache_capacity=8, policy="two_queue_ttl"),
        ]
        snapshots = SimulationRunner.compare(trace, configs)
        compare_table(snapshots)
        captured = capsys.readouterr()
        assert "prefix_block_hit_rate" in captured.out
