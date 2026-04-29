from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Set


class BlockQueue(str, Enum):
    """Logical queue a FREE_CACHED block belongs to."""

    PROBATION = "probation"
    PROTECTED = "protected"
    HARD_PROTECTED = "hard_protected"   # reserved for Phase 2


@dataclass
class BlockMeta:
    """Metadata tracked per FREE_CACHED block throughout its lifetime.

    This is the single source of truth for eviction decisions.
    All fields are updated in-place by the policy on each access/eviction.

    block_key:    Prefix-path hex key (sha256 chain). Used as the cache dict key.
    content_hash: Original hash_ids[i] from the trace (raw block content hash).
                  Preserved for non-prefix potential analysis and debugging.
    """

    block_key: str
    block_pos: int              # 0-based position in the prefix chain of the inserting request
    entry_time: float           # wall-clock seconds when block entered the FREE_CACHED pool
    content_hash: str = ""      # raw hash_ids[i]; empty when not provided by caller
    hit_count: int = 0
    queue: BlockQueue = BlockQueue.PROBATION
    ttl_expiry: Optional[float] = None      # None → no TTL protection (Probation default)
    users_seen: Set[str] = field(default_factory=set)

    def is_ttl_expired(self, current_time: float) -> bool:
        """Return True if this block's TTL protection has lapsed."""
        if self.ttl_expiry is None:
            return True
        return current_time >= self.ttl_expiry

    @property
    def is_hot(self) -> bool:
        """Shorthand: has this block been verified as reusable (hit ≥ 2)?"""
        return self.hit_count >= 2


@dataclass
class PolicyEvents:
    """Side-channel events emitted by a policy during a single request.

    Policies accumulate these and return them via flush_events() so the
    MetricsCollector can track promotions/demotions without coupling to
    policy internals.
    """

    promotions: List[str] = field(default_factory=list)    # block hashes promoted
    demotions: List[str] = field(default_factory=list)     # block hashes demoted
