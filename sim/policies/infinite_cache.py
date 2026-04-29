"""InfiniteCachePolicy — S0 strategy: no capacity limit, never evicts.

Purpose
-------
Gives the ideal_hit_rate upper bound for a fixed routing configuration.
Used as the denominator of gap_closed_ratio:

    gap_closed_ratio = (policy_hit_rate - lru_hit_rate)
                     / (infinite_hit_rate - lru_hit_rate)

The gap between infinite_cache and LRU represents the total potential
improvement attributable to eviction quality (not routing).
"""
from __future__ import annotations

from typing import Optional, Set

from ..core.block import BlockMeta, BlockQueue
from ..core.policy import AbstractCachePolicy


class InfiniteCachePolicy(AbstractCachePolicy):
    """Infinite-capacity cache: blocks are never evicted (S0)."""

    def __init__(self, capacity: int) -> None:
        # Ignore the supplied capacity — this policy never fills up.
        super().__init__(capacity=2 ** 31)
        self._cache: dict[str, BlockMeta] = {}

    @property
    def size(self) -> int:
        return len(self._cache)

    def is_full(self) -> bool:
        return False

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
        return None
