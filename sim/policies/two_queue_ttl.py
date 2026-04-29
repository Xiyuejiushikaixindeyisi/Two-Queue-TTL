"""Two-Queue TTL eviction policy — Phase 1 MVP implementation.

Design references
-----------------
  kv_cache_eviction_design.md §3, §6, §7, §10–12
  two_queue_ttl_experiment_plan.md strategy S5/S6/S7

Queue layout (FREE_CACHED pool only)
-------------------------------------
  Probation  — first-time / unverified blocks
  Protected  — hit_count ≥ promotion_threshold or registry match; TTL-protected

Eviction priority order
------------------------
  1. Protected: TTL-expired blocks, LRU order  (stale protected cleared first)
  2. Probation: LRU head (oldest)
  3. Protected: non-expired blocks, LRU order (last resort)

Rationale for Priority 1 = expired Protected:
  Probation blocks may be new session content about to be reused.
  TTL-expired Protected blocks are stale (session ended > base_ttl ago).
  Evicting stale Protected before Probation prevents Protected from bloating
  with dead sessions while valuable new session blocks are forced out.

TTL anchors (locked to F13 reuse_time data)
--------------------------------------------
  base_ttl     = 46 s   (F13 p80 — covers 80% of reuse events)
  extended_ttl = 255 s  (F13 p95 — for hit_count ≥ 5 blocks)
"""
from __future__ import annotations

from collections import OrderedDict
from typing import List, Optional, Set

from ..core.block import BlockMeta, BlockQueue, PolicyEvents
from ..core.policy import AbstractCachePolicy
from ..io.registry import OfflineRegistry


class TwoQueueTTLPolicy(AbstractCachePolicy):
    """Probation / Protected dual-queue policy with TTL-based soft protection.

    Parameters
    ----------
    capacity:
        Total block slots across both queues.
    base_ttl:
        TTL (seconds) assigned when a block enters Protected.
    extended_ttl:
        TTL (seconds) used when hit_count ≥ 5.
    promotion_threshold:
        Minimum hit_count for Probation → Protected promotion.
    protected_ratio:
        Fraction of total capacity reserved for Protected blocks.
        When Protected grows beyond this cap, the LRU Protected block is
        demoted to Probation.  This keeps probation large enough to absorb
        cache-pollution bursts without evicting Protected blocks.
    registry:
        Optional offline registry.  Matching blocks bypass Probation
        and enter Protected immediately on first insertion.
    """

    def __init__(
        self,
        capacity: int,
        base_ttl: float = 46.0,
        extended_ttl: float = 255.0,
        promotion_threshold: int = 2,
        protected_ratio: float = 0.30,
        registry: Optional[OfflineRegistry] = None,
    ) -> None:
        super().__init__(capacity)
        self._base_ttl = base_ttl
        self._extended_ttl = extended_ttl
        self._promotion_threshold = promotion_threshold
        self._protected_capacity = max(1, int(capacity * protected_ratio))
        self._registry = registry

        self._probation: OrderedDict[str, BlockMeta] = OrderedDict()
        self._protected: OrderedDict[str, BlockMeta] = OrderedDict()

        # Event buffer flushed by flush_events()
        self._pending_promotions: List[str] = []
        self._pending_demotions: List[str] = []

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self._probation) + len(self._protected)

    @property
    def probation_size(self) -> int:
        return len(self._probation)

    @property
    def protected_size(self) -> int:
        return len(self._protected)

    # ------------------------------------------------------------------
    # AbstractCachePolicy implementation
    # ------------------------------------------------------------------

    def contains(self, block_key: str) -> bool:
        return block_key in self._probation or block_key in self._protected

    def access(
        self,
        block_key: str,
        timestamp: float,
        block_pos: int,
        user_id: str,
    ) -> bool:
        if block_key in self._probation:
            meta = self._probation[block_key]
            meta.hit_count += 1
            meta.users_seen.add(user_id)
            if meta.hit_count >= self._promotion_threshold:
                self._promote(block_key, meta, timestamp)
            else:
                self._probation.move_to_end(block_key)
            return True

        if block_key in self._protected:
            meta = self._protected[block_key]
            meta.hit_count += 1
            meta.users_seen.add(user_id)
            self._refresh_ttl(meta, timestamp)
            self._protected.move_to_end(block_key)
            return True

        return False

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
        )
        meta.users_seen.add(user_id)

        if self._registry and self._registry.is_protected(block_key):
            meta.queue = BlockQueue.PROTECTED
            self._refresh_ttl(meta, timestamp)
            self._protected[block_key] = meta
            self._protected.move_to_end(block_key)
            self._maybe_demote_overflow()
        else:
            meta.queue = BlockQueue.PROBATION
            self._probation[block_key] = meta
            self._probation.move_to_end(block_key)

    def evict_one(
        self,
        timestamp: float,
        pinned: Optional[Set[str]] = None,
    ) -> Optional[BlockMeta]:
        # Priority 1: Protected — TTL-expired, LRU order (stale blocks first)
        evicted = self._evict_expired_from(self._protected, timestamp, pinned)
        if evicted is not None:
            return evicted

        # Priority 2: Probation LRU head
        evicted = self._evict_from(self._probation, timestamp, pinned)
        if evicted is not None:
            return evicted

        # Priority 3: Protected — non-expired blocks, LRU order (last resort)
        evicted = self._evict_from(self._protected, timestamp, pinned)
        return evicted

    def flush_events(self) -> PolicyEvents:
        events = PolicyEvents(
            promotions=self._pending_promotions,
            demotions=self._pending_demotions,
        )
        self._pending_promotions = []
        self._pending_demotions = []
        return events

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh_ttl(self, meta: BlockMeta, timestamp: float) -> None:
        ttl = self._extended_ttl if meta.hit_count >= 5 else self._base_ttl
        meta.ttl_expiry = timestamp + ttl

    def _promote(self, block_key: str, meta: BlockMeta, timestamp: float) -> None:
        del self._probation[block_key]
        meta.queue = BlockQueue.PROTECTED
        self._refresh_ttl(meta, timestamp)
        self._protected[block_key] = meta
        self._protected.move_to_end(block_key)
        self._pending_promotions.append(block_key)
        self._maybe_demote_overflow()

    def _maybe_demote_overflow(self) -> None:
        """Demote LRU Protected block to Probation when Protected exceeds its cap.

        Enforcing the cap keeps probation large enough to absorb pollution bursts
        without having to evict non-expired Protected blocks.
        """
        while len(self._protected) > self._protected_capacity:
            block_key, meta = next(iter(self._protected.items()))
            del self._protected[block_key]
            meta.queue = BlockQueue.PROBATION
            self._probation[block_key] = meta
            self._probation.move_to_end(block_key)
            self._pending_demotions.append(block_key)

    @staticmethod
    def _evict_from(
        queue: OrderedDict[str, BlockMeta],
        timestamp: float,
        pinned: Optional[Set[str]],
    ) -> Optional[BlockMeta]:
        for block_key, meta in queue.items():
            if pinned and block_key in pinned:
                continue
            del queue[block_key]
            return meta
        return None

    @staticmethod
    def _evict_expired_from(
        queue: OrderedDict[str, BlockMeta],
        timestamp: float,
        pinned: Optional[Set[str]],
    ) -> Optional[BlockMeta]:
        for block_key, meta in queue.items():
            if pinned and block_key in pinned:
                continue
            if meta.is_ttl_expired(timestamp):
                del queue[block_key]
                return meta
        return None
