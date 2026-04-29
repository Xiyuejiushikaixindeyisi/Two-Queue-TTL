"""SimulationRunner — orchestrates a full simulation run.

Typical usage
-------------
    config = SimConfig(cache_capacity=10_000, policy="two_queue_ttl")
    runner = SimulationRunner(config)
    snapshot = runner.run(trace)

Multi-policy comparison
-----------------------
    snapshots = SimulationRunner.compare(
        trace,
        configs=[
            SimConfig(cache_capacity=10_000, policy="lru"),
            SimConfig(cache_capacity=10_000, policy="infinite_cache"),
            SimConfig(cache_capacity=10_000, policy="belady"),
            SimConfig(cache_capacity=10_000, policy="two_queue_ttl"),
        ],
    )
    compare_table(snapshots)

Belady Oracle note
------------------
BeladyOraclePolicy requires a FutureIndex (block_key → sorted timestamps).
SimulationRunner builds this index automatically when policy == "belady".
The same trace passed to run() is used to build the index, so callers
need no special handling.
"""
from __future__ import annotations

from typing import List, Optional

from .cache.prefix_cache import PrefixCache
from .config import SimConfig, TTLLRUConfig
from .core.future_index import FutureIndex, build_future_index  # noqa: F401 (FutureIndex used in type hint)
from .core.trace import TraceRecord
from .io.registry import OfflineRegistry
from .metrics.collector import MetricsCollector, MetricsSnapshot
from .policies import POLICY_REGISTRY


class SimulationRunner:
    """Runs one policy against a trace and returns a MetricsSnapshot.

    Parameters
    ----------
    config:
        Simulation configuration (policy, capacity, block_size, …).
    registry:
        Optional pre-loaded OfflineRegistry.  If None and
        config.registry_path is set, the registry is loaded from disk.
    """

    def __init__(
        self,
        config: SimConfig,
        registry: Optional[OfflineRegistry] = None,
    ) -> None:
        self._config = config
        self._registry = registry or self._load_registry(config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        trace: List[TraceRecord],
        future_index: Optional[FutureIndex] = None,
    ) -> MetricsSnapshot:
        """Replay the trace and return the collected metrics snapshot.

        Parameters
        ----------
        future_index:
            Pre-built future access index.  When compare() is used, pass the
            shared index to avoid rebuilding it once per policy.
        """
        if future_index is None:
            future_index = build_future_index(trace)

        policy = self._build_policy(future_index)
        cache = PrefixCache(policy, block_size=self._config.block_size)
        collector = MetricsCollector(
            policy_name=self._config.policy,
            capacity=self._config.cache_capacity,
            block_size=self._config.block_size,
        )
        collector.set_future_index(future_index)

        for record in trace:
            result = cache.process_request(record)
            collector.on_request(result)

        return collector.finalize()

    @classmethod
    def compare(
        cls,
        trace: List[TraceRecord],
        configs: List[SimConfig],
        registry: Optional[OfflineRegistry] = None,
    ) -> List[MetricsSnapshot]:
        """Run multiple policies against the same trace and return all snapshots.

        Builds the future index once and shares it across all policy runs to
        avoid O(policies) redundant SHA-256 chain recomputations.
        """
        shared_index = build_future_index(trace)
        return [cls(cfg, registry=registry).run(trace, future_index=shared_index)
                for cfg in configs]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_policy(self, future_index: FutureIndex):
        name = self._config.policy
        if name not in POLICY_REGISTRY:
            raise ValueError(
                f"Unknown policy {name!r}. Available: {list(POLICY_REGISTRY)}"
            )
        cls = POLICY_REGISTRY[name]

        if name == "belady":
            return cls(capacity=self._config.cache_capacity, future_index=future_index)

        if name == "two_queue_ttl":
            ttl_cfg = self._config.two_queue_ttl
            return cls(
                capacity=self._config.cache_capacity,
                base_ttl=ttl_cfg.base_ttl,
                extended_ttl=ttl_cfg.extended_ttl,
                promotion_threshold=ttl_cfg.promotion_threshold,
                protected_ratio=ttl_cfg.protected_ratio,
                registry=self._registry,
            )

        if name == "ttl_lru":
            return cls(capacity=self._config.cache_capacity, ttl=self._config.ttl_lru.ttl)

        # lru, infinite_cache — only capacity argument
        return cls(capacity=self._config.cache_capacity)

    @staticmethod
    def _load_registry(config: SimConfig) -> Optional[OfflineRegistry]:
        if config.registry_path:
            return OfflineRegistry.from_files(protected_path=config.registry_path)
        return OfflineRegistry.empty()
