"""Abstract base class for all eviction policies.

Extension guide
---------------
To add a new policy (e.g. Belady oracle):

  1. Create sim/policies/belady.py and subclass AbstractCachePolicy.
  2. Implement the four abstract methods.
  3. Register the class in sim/policies/__init__.py::POLICY_REGISTRY.

The rest of the system (PrefixCache, MetricsCollector, SimulationRunner)
requires no changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Set

from .block import BlockMeta, PolicyEvents


class AbstractCachePolicy(ABC):
    """Manages the FREE_CACHED block pool for one simulated cache instance.

    Lifecycle contract
    ------------------
    * access()   — called when the request lookup finds a block in cache.
                   Updates internal state; returns True (hit).
                   If block is absent, returns False (miss) without side effects.
    * add()      — called after the caller has ensured capacity (via evict_one loops).
                   Inserts the block into the pool.
    * evict_one() — called when the pool is full and space is needed.
                    Returns the evicted BlockMeta for metrics accounting.
                    Skips any block in the `pinned` set (blocks active in current request).
    * flush_events() — returns policy-specific promotion/demotion events since the
                       last flush and resets the internal event buffer.

    Invariants the policy MUST preserve
    ------------------------------------
    * Never evict a block present in the caller-supplied `pinned` set.
    * never evict a block that was added in the same request (tracked via pinned).
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    @abstractmethod
    def size(self) -> int:
        """Current number of blocks in the managed pool."""

    def is_full(self) -> bool:
        return self.size >= self._capacity

    # ------------------------------------------------------------------
    # Core operations (must be overridden)
    # ------------------------------------------------------------------

    @abstractmethod
    def contains(self, block_key: str) -> bool:
        """Return True if block is currently in the pool (O(1) check)."""

    @abstractmethod
    def access(
        self,
        block_key: str,
        timestamp: float,
        block_pos: int,
        user_id: str,
    ) -> bool:
        """Process a block access event.

        Returns True (hit) if the block was in the pool and updates internal
        state (LRU order, hit_count, TTL refresh, potential promotion).
        Returns False (miss) if the block is absent; no state change.
        """

    @abstractmethod
    def add(
        self,
        block_key: str,
        content_hash: str,
        timestamp: float,
        block_pos: int,
        user_id: str,
    ) -> None:
        """Insert a new block into the pool.

        block_key:    Prefix-path hex key (used as dict key).
        content_hash: Original hash_ids[i] stored in BlockMeta for diagnostics.
        The caller guarantees the pool is not full before calling this.
        """

    @abstractmethod
    def evict_one(
        self,
        timestamp: float,
        pinned: Optional[Set[str]] = None,
    ) -> Optional[BlockMeta]:
        """Evict one block according to the policy.

        Parameters
        ----------
        timestamp:
            Current simulation time (used for TTL expiry checks).
        pinned:
            Block hashes that must not be evicted in this call.

        Returns the evicted BlockMeta (for metrics), or None if no
        evictable block exists (should only happen when all blocks are pinned).
        """

    # ------------------------------------------------------------------
    # Optional hook (override in policies that track internal events)
    # ------------------------------------------------------------------

    def flush_events(self) -> PolicyEvents:
        """Return and clear accumulated policy events (promotions, demotions).

        Default implementation returns an empty event set.
        TwoQueueTTLPolicy overrides this to report actual promotions.
        """
        return PolicyEvents()
