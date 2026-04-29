"""Metric accumulation for one simulation run.

Two-pass design
---------------
Pass 1 (online, during replay):
    on_request() accumulates raw counters from each RequestResult.

Pass 2 (offline, after replay):
    finalize() runs post-processing that requires future knowledge:
    - evicted_before_next_hit_count: did the evicted block appear later in trace?
    - protected_pollution_rate: did protected-queue blocks ever get hit again?

The reason for two passes: these metrics require knowing what happens
*after* an eviction, which is only available once the full trace is replayed.

gap_closed_ratio
----------------
Requires knowing the LRU and Infinite Cache hit rates for the same trace.
Call compute_gap_closed_ratios(snapshots) after collecting all policy results.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..cache.prefix_cache import RequestResult
from ..core.block import BlockMeta, BlockQueue
from ..core.future_index import FutureIndex, build_future_index, has_future_access
from ..core.trace import TraceRecord


@dataclass
class MetricsSnapshot:
    """Immutable result object produced by MetricsCollector.finalize().

    Metric definitions align with experiment plan §2.2 and §5.5.
    """

    policy_name: str
    cache_capacity: int
    block_size: int

    # Counters set during finalize()
    total_requests: int = 0
    total_blocks_requested: int = 0
    total_blocks_hit: int = 0
    total_blocks_miss: int = 0
    saved_prefill_tokens: int = 0

    eviction_count: int = 0
    hot_prefix_eviction_count: int = 0         # evicted with hit_count ≥ 2
    evicted_before_next_hit_count: int = 0     # had a future access but were evicted

    protected_eviction_count: int = 0
    probation_eviction_count: int = 0
    protected_polluted_eviction_count: int = 0  # entered Protected, never hit again

    promotion_count: int = 0

    # Set by compute_gap_closed_ratios() after all policies are collected.
    # gap_closed_ratio = (this_hit_rate - lru_hit_rate) / (infinite_hit_rate - lru_hit_rate)
    gap_closed_ratio: Optional[float] = None

    # ------------------------------------------------------------------
    # Derived metrics (computed properties, not stored)
    # ------------------------------------------------------------------

    @property
    def prefix_block_hit_rate(self) -> float:
        if self.total_blocks_requested == 0:
            return 0.0
        return self.total_blocks_hit / self.total_blocks_requested

    @property
    def protected_pollution_rate(self) -> float:
        """Fraction of Protected evictions where the block was never hit again.

        High values (> 30%) indicate the promotion criterion is too permissive.
        """
        if self.protected_eviction_count == 0:
            return 0.0
        return self.protected_polluted_eviction_count / self.protected_eviction_count

    @property
    def policy(self) -> str:
        return self.policy_name

    def to_dict(self) -> dict:
        d = {
            "policy": self.policy_name,
            "cache_capacity": self.cache_capacity,
            "block_size": self.block_size,
            "total_requests": self.total_requests,
            "total_blocks_requested": self.total_blocks_requested,
            "total_blocks_hit": self.total_blocks_hit,
            "total_blocks_miss": self.total_blocks_miss,
            "prefix_block_hit_rate": round(self.prefix_block_hit_rate, 6),
            "saved_prefill_tokens": self.saved_prefill_tokens,
            "eviction_count": self.eviction_count,
            "hot_prefix_eviction_count": self.hot_prefix_eviction_count,
            "evicted_before_next_hit_count": self.evicted_before_next_hit_count,
            "protected_eviction_count": self.protected_eviction_count,
            "probation_eviction_count": self.probation_eviction_count,
            "protected_pollution_rate": round(self.protected_pollution_rate, 6),
            "promotion_count": self.promotion_count,
        }
        if self.gap_closed_ratio is not None:
            d["gap_closed_ratio"] = round(self.gap_closed_ratio, 6)
        return d


def compute_gap_closed_ratios(snapshots: List[MetricsSnapshot]) -> None:
    """Compute gap_closed_ratio in-place for all snapshots.

    gap_closed_ratio = (policy_hit_rate - lru_hit_rate)
                     / (infinite_hit_rate - lru_hit_rate)

    Requires at least one "lru" and one "infinite_cache" snapshot in the list.
    If either is missing the metric is left as None.

    Range:
      0.0  = same as LRU
      1.0  = matches Infinite Cache (impossible in finite cache but Belady gets close)
      < 0  = worse than LRU (should not happen with a well-implemented policy)
    """
    lru_rate = next(
        (s.prefix_block_hit_rate for s in snapshots if s.policy_name == "lru"), None
    )
    inf_rate = next(
        (s.prefix_block_hit_rate for s in snapshots if s.policy_name == "infinite_cache"),
        None,
    )
    if lru_rate is None or inf_rate is None:
        return
    denom = inf_rate - lru_rate
    if denom <= 0:
        return
    for s in snapshots:
        s.gap_closed_ratio = (s.prefix_block_hit_rate - lru_rate) / denom


class MetricsCollector:
    """Accumulates raw events during replay, then computes derived metrics.

    Usage
    -----
    collector = MetricsCollector("lru", capacity=10_000, block_size=128)
    collector.set_future_index(future_index)   # required before replay

    for record in trace:
        result = cache.process_request(record)
        collector.on_request(result)

    snapshot = collector.finalize()
    """

    def __init__(self, policy_name: str, capacity: int, block_size: int) -> None:
        self._policy_name = policy_name
        self._capacity = capacity
        self._block_size = block_size

        # Online counters (accumulated during replay)
        self._total_requests = 0
        self._total_blocks_requested = 0
        self._total_blocks_hit = 0
        self._total_blocks_miss = 0
        self._saved_prefill_tokens = 0
        self._promotion_count = 0

        # Eviction log for post-processing
        # Each entry: (block_key, eviction_time, hit_count, queue)
        self._eviction_log: List[tuple] = []

        # Future access index: block_key (hex) → sorted list of access timestamps.
        # Set by set_future_index() before replay.
        self._future_index: Optional[FutureIndex] = None

    # ------------------------------------------------------------------
    # Pre-processing (call once before replay)
    # ------------------------------------------------------------------

    def set_future_index(self, future_index: FutureIndex) -> None:
        """Inject a pre-built FutureIndex (shared with BeladyOraclePolicy)."""
        self._future_index = future_index

    def precompute_future_access(self, trace: List[TraceRecord]) -> None:
        """Build and set a FutureIndex from the trace (convenience wrapper)."""
        self._future_index = build_future_index(trace)

    # ------------------------------------------------------------------
    # Online accumulation (call per request during replay)
    # ------------------------------------------------------------------

    def on_request(self, result: RequestResult) -> None:
        self._total_requests += 1
        self._total_blocks_requested += len(result.record.hash_ids)
        self._total_blocks_hit += result.hit_count
        self._total_blocks_miss += result.miss_count
        self._saved_prefill_tokens += result.hit_count * self._block_size
        self._promotion_count += len(result.events.promotions)

        for meta in result.evicted_blocks:
            self._eviction_log.append(
                (meta.block_key, result.record.timestamp, meta.hit_count, meta.queue)
            )

    # ------------------------------------------------------------------
    # Post-processing (call once after replay)
    # ------------------------------------------------------------------

    def finalize(self) -> MetricsSnapshot:
        snapshot = MetricsSnapshot(
            policy_name=self._policy_name,
            cache_capacity=self._capacity,
            block_size=self._block_size,
            total_requests=self._total_requests,
            total_blocks_requested=self._total_blocks_requested,
            total_blocks_hit=self._total_blocks_hit,
            total_blocks_miss=self._total_blocks_miss,
            saved_prefill_tokens=self._saved_prefill_tokens,
            promotion_count=self._promotion_count,
        )

        for block_key, eviction_time, hit_count, queue in self._eviction_log:
            snapshot.eviction_count += 1

            if hit_count >= 2:
                snapshot.hot_prefix_eviction_count += 1

            if queue == BlockQueue.PROTECTED:
                snapshot.protected_eviction_count += 1
                if self._future_index is not None:
                    if not has_future_access(self._future_index, block_key, eviction_time):
                        snapshot.protected_polluted_eviction_count += 1
            else:
                snapshot.probation_eviction_count += 1

            if self._future_index is not None:
                if has_future_access(self._future_index, block_key, eviction_time):
                    snapshot.evicted_before_next_hit_count += 1

        return snapshot
