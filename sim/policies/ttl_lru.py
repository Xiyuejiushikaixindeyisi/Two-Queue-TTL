"""TTL-LRU policy — LRU with a uniform TTL on all blocks.

Planned for Phase 2 ablation (isolates the pure TTL contribution
independent of Probation/Protected queue separation).

Strategy ID S3 in experiment plan section 5.2.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Optional, Set

from ..core.block import BlockMeta, BlockQueue
from ..core.policy import AbstractCachePolicy


class TTLLRUPolicy(AbstractCachePolicy):
    """LRU where blocks expire after a fixed TTL even if not evicted.

    Expired blocks are treated as misses on next access and are the
    first candidates for eviction.

    Parameters
    ----------
    ttl:
        Seconds a block is considered valid after its last access.
        Defaults to 46 s (F13 p80 anchor).
    """

    def __init__(self, capacity: int, ttl: float = 46.0) -> None:
        super().__init__(capacity)
        self._ttl = ttl
        self._cache: OrderedDict[str, BlockMeta] = OrderedDict()
        # Lower-bound on the minimum ttl_expiry across all cached blocks.
        # If _min_expiry > timestamp, no block is expired — skip the O(n) scan.
        # Never updated on eviction (stays as a safe lower bound even when stale).
        self._min_expiry: float = float("inf")

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
        # TTL expiry does NOT cause a miss.  TTL only affects evict_one() priority.
        meta = self._cache[block_key]
        meta.hit_count += 1
        meta.users_seen.add(user_id)
        meta.ttl_expiry = timestamp + self._ttl   # refresh on every hit
        self._cache.move_to_end(block_key)
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
            ttl_expiry=timestamp + self._ttl,
            queue=BlockQueue.PROBATION,
        )
        meta.users_seen.add(user_id)
        self._cache[block_key] = meta
        self._cache.move_to_end(block_key)
        if meta.ttl_expiry < self._min_expiry:
            self._min_expiry = meta.ttl_expiry

    def evict_one(
        self,
        timestamp: float,
        pinned: Optional[Set[str]] = None,
    ) -> Optional[BlockMeta]:
        # Fast path: skip the O(n) expired scan when _min_expiry proves no block
        # has expired yet.  _min_expiry is a safe lower bound: it may be stale
        # (too low) after evictions but can never be higher than the true minimum,
        # so "timestamp < _min_expiry" reliably means zero expired blocks.
        if self._min_expiry <= timestamp:
            for block_key, meta in self._cache.items():
                if pinned and block_key in pinned:
                    continue
                if meta.is_ttl_expired(timestamp):
                    del self._cache[block_key]
                    return meta

        for block_key, meta in self._cache.items():
            if pinned and block_key in pinned:
                continue
            del self._cache[block_key]
            return meta

        return None
