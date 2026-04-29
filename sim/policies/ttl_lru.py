"""TTL-LRU policy — LRU with a uniform TTL on all blocks.

Planned for Phase 2 ablation (isolates the pure TTL contribution
independent of Probation/Protected queue separation).

Strategy ID S3 in experiment plan section 5.2.
"""
from __future__ import annotations

import heapq
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
        # Min-heap of (ttl_expiry, block_key).  Lazy: entries may be stale
        # (block evicted or TTL refreshed).  Validated on pop via:
        #   key not in _cache  → evicted, skip
        #   cache[key].ttl_expiry != heap_exp  → refreshed, skip
        self._expiry_heap: list = []
        # Legacy lower-bound kept for the unit-test that directly mutates
        # ttl_expiry and syncs _min_expiry.  Not used by evict_one() anymore.
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
        # Push a fresh entry; the old one becomes stale and will be skipped on pop.
        heapq.heappush(self._expiry_heap, (meta.ttl_expiry, block_key))
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
        heapq.heappush(self._expiry_heap, (meta.ttl_expiry, block_key))
        if meta.ttl_expiry < self._min_expiry:
            self._min_expiry = meta.ttl_expiry

    def evict_one(
        self,
        timestamp: float,
        pinned: Optional[Set[str]] = None,
    ) -> Optional[BlockMeta]:
        # Phase 1: pop expired entries from the heap (O(log n) per pop).
        # Lazy deletion: skip stale entries (evicted or TTL-refreshed blocks).
        skipped_pinned: list = []
        result: Optional[BlockMeta] = None

        while self._expiry_heap:
            exp, key = self._expiry_heap[0]
            # Stale: block was evicted already.
            if key not in self._cache:
                heapq.heappop(self._expiry_heap)
                continue
            # Stale: TTL was refreshed after this heap entry was pushed.
            if self._cache[key].ttl_expiry != exp:
                heapq.heappop(self._expiry_heap)
                continue
            # Not expired yet — no expired blocks remain.
            if exp > timestamp:
                break
            heapq.heappop(self._expiry_heap)
            if pinned and key in pinned:
                skipped_pinned.append((exp, key))
                continue
            result = self._cache[key]
            del self._cache[key]
            break

        # Re-push pinned-but-expired entries so they remain evictable later.
        for item in skipped_pinned:
            heapq.heappush(self._expiry_heap, item)

        if result is not None:
            return result

        # Phase 2: LRU fallback — no expired non-pinned block found.
        for block_key, meta in self._cache.items():
            if pinned and block_key in pinned:
                continue
            del self._cache[block_key]
            return meta

        return None
