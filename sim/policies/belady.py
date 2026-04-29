"""BeladyOraclePolicy — S2 strategy: theoretically optimal eviction.

Belady's algorithm evicts the block whose next access is farthest in the
future (or has no future access at all).  It requires omniscient knowledge
of the future and is unimplementable online, but provides the tightest
achievable upper bound for any online policy at the same capacity.

Usage
-----
The policy is constructed by SimulationRunner when policy == "belady".
The runner pre-builds the FutureIndex from the trace and passes it here.
No other caller needs to instantiate this class directly.

gap_closed_ratio interpretation
---------------------------------
    gap_closed_ratio(belady) ≤ 1.0  (Belady is also capacity-limited)
    gap_closed_ratio(two_queue_ttl) ideally ≥ 0.5

The gap between Belady and Infinite Cache reflects losses that no eviction
policy can recover (blocks that would have been reused but there was simply
no room even with perfect eviction).

Known limitation: flat-cache vs. prefix-cache semantics
---------------------------------------------------------
This implementation is a flat-cache Belady: it evicts the block whose
individual next_access is farthest in the future, without considering the
chain structure.  In a prefix cache, position-0 blocks are more valuable
than position-N blocks because evicting a position-0 block makes ALL
subsequent blocks in that request into misses (chain break cascade).

As a result, on very small traces (< 100 requests), Belady may
occasionally score BELOW LRU because ties in next_access are broken by
iteration order rather than chain importance.  On large production traces
(thousands of requests), this effect is negligible and Belady will
reliably outperform LRU, providing a useful algorithmic upper bound.
"""
from __future__ import annotations

from typing import Optional, Set

from ..core.block import BlockMeta, BlockQueue
from ..core.future_index import FutureIndex, next_access_after
from ..core.policy import AbstractCachePolicy


class BeladyOraclePolicy(AbstractCachePolicy):
    """Optimal offline eviction: always evict the block with farthest next access (S2)."""

    def __init__(self, capacity: int, future_index: FutureIndex) -> None:
        super().__init__(capacity)
        self._future_index = future_index
        self._cache: dict[str, BlockMeta] = {}

    @property
    def size(self) -> int:
        return len(self._cache)

    def contains(self, block_key: str) -> bool:
        return block_key in self._cache

    def access(
        self,
        block_key: str,
        timestamp: float,
        block_pos: int,
        user_id: str,
    ) -> bool:
        if block_key not in self._cache:
            return False
        meta = self._cache[block_key]
        meta.hit_count += 1
        meta.users_seen.add(user_id)
        return True

    def add(
        self,
        block_key: str,
        content_hash: str,
        timestamp: float,
        block_pos: int,
        user_id: str,
    ) -> None:
        meta = BlockMeta(
            block_key=block_key,
            content_hash=content_hash,
            block_pos=block_pos,
            entry_time=timestamp,
            queue=BlockQueue.PROBATION,
        )
        meta.users_seen.add(user_id)
        self._cache[block_key] = meta

    def evict_one(
        self,
        timestamp: float,
        pinned: Optional[Set[str]] = None,
    ) -> Optional[BlockMeta]:
        best_key: Optional[str] = None
        best_next: float = -1.0

        for block_key in self._cache:
            if pinned and block_key in pinned:
                continue
            t_next = next_access_after(self._future_index, block_key, timestamp)
            if t_next > best_next:
                best_next = t_next
                best_key = block_key
                if t_next == float("inf"):
                    break  # No future access = guaranteed best choice; stop early

        if best_key is None:
            return None
        return self._cache.pop(best_key)
