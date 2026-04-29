from __future__ import annotations

from collections import OrderedDict
from typing import Optional, Set

from ..core.block import BlockMeta, BlockQueue
from ..core.policy import AbstractCachePolicy


class LRUPolicy(AbstractCachePolicy):
    """Plain LRU eviction — the production baseline.

    All FREE_CACHED blocks share a single OrderedDict.
    Most-recently-used blocks are at the tail; eviction takes from the head.
    No TTL, no queue distinction.
    """

    def __init__(self, capacity: int) -> None:
        super().__init__(capacity)
        self._cache: OrderedDict[str, BlockMeta] = OrderedDict()

    # ------------------------------------------------------------------
    # AbstractCachePolicy implementation
    # ------------------------------------------------------------------

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
            queue=BlockQueue.PROBATION,
        )
        meta.users_seen.add(user_id)
        self._cache[block_key] = meta
        self._cache.move_to_end(block_key)

    def evict_one(
        self,
        timestamp: float,
        pinned: Optional[Set[str]] = None,
    ) -> Optional[BlockMeta]:
        for block_key, meta in self._cache.items():
            if pinned and block_key in pinned:
                continue
            del self._cache[block_key]
            return meta
        return None
