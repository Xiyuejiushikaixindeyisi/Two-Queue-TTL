"""Prefix cache simulation engine.

Prefix-cache semantics
----------------------
A request's prefix chain is its hash_ids list ordered from position 0.
The cache holds a hit on position i **only if** positions 0 … i-1 all hit too.
The first miss breaks the prefix chain; all subsequent blocks are misses.

Simulation of ref_cnt / pinning
--------------------------------
In vLLM, blocks currently used by a request have ref_cnt > 0 and cannot
be evicted.  We model this with a `pinned` set that grows as we allocate
miss blocks for the current request:

  • Hit blocks are added to `pinned` at the start of each request.
  • Each new miss block is pinned before its allocation so the eviction
    loop never immediately evicts a block we just inserted.

This mirrors the invariant: "never evict a block belonging to the current
request" (design doc §4.2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from ..core.block import BlockMeta, PolicyEvents
from ..core.policy import AbstractCachePolicy
from ..core.prefix_key import make_prefix_path_keys
from ..core.trace import TraceRecord


@dataclass
class RequestResult:
    """Outcome of processing one TraceRecord through the prefix cache."""

    record: TraceRecord
    hit_block_hashes: List[str]
    miss_block_hashes: List[str]
    evicted_blocks: List[BlockMeta]
    events: PolicyEvents = field(default_factory=PolicyEvents)

    @property
    def hit_count(self) -> int:
        return len(self.hit_block_hashes)

    @property
    def miss_count(self) -> int:
        return len(self.miss_block_hashes)

    @property
    def eviction_count(self) -> int:
        return len(self.evicted_blocks)


class PrefixCache:
    """Stateful prefix-cache simulator backed by an eviction policy.

    This class is policy-agnostic: it delegates all eviction decisions to
    whatever AbstractCachePolicy implementation is injected.

    Parameters
    ----------
    policy:
        The eviction policy that manages the FREE_CACHED block pool.
    block_size:
        Tokens per block (used to compute saved_prefill_tokens in results).
    """

    def __init__(self, policy: AbstractCachePolicy, block_size: int = 128) -> None:
        self._policy = policy
        self._block_size = block_size

    @property
    def policy(self) -> AbstractCachePolicy:
        return self._policy

    # ------------------------------------------------------------------
    # Core simulation step
    # ------------------------------------------------------------------

    def process_request(self, record: TraceRecord) -> RequestResult:
        """Simulate one request against the current cache state.

        Algorithm
        ---------
        1. Walk hash_ids in order; collect the longest consecutive hit prefix.
        2. For each miss block: evict until there is capacity, then add.
        3. Ask the policy for side-channel events (promotions/demotions).
        4. Return a RequestResult for the MetricsCollector.
        """
        timestamp = record.timestamp
        hit_blocks: List[str] = []
        miss_blocks: List[str] = []
        evicted: List[BlockMeta] = []

        # Compute chained prefix-path keys (mirrors vLLM's hash_block_tokens chain).
        # Using raw hash_ids[i] as keys is incorrect: two requests that share the
        # same block content at position i but differ earlier would collide.
        prefix_keys = make_prefix_path_keys(record.model_id, record.hash_ids)

        # ---- Phase 1: find longest prefix hit ----
        for pos, key in enumerate(prefix_keys):
            key_str = key.hex()
            if self._policy.access(key_str, timestamp, pos, record.user_id):
                hit_blocks.append(key_str)
            else:
                break   # prefix semantics: chain broken at first miss

        # ---- Phase 2: allocate miss blocks ----
        # `pinned` starts with all hit blocks (they're "active" in this request)
        pinned = set(hit_blocks)
        prefix_break = len(hit_blocks)

        for pos in range(prefix_break, len(prefix_keys)):
            key_str = prefix_keys[pos].hex()

            # Pin this block before allocation so it can't be its own eviction victim
            pinned.add(key_str)

            # Make space if needed
            while self._policy.is_full():
                evicted_meta = self._policy.evict_one(timestamp, pinned)
                if evicted_meta is None:
                    # All remaining blocks are pinned — no room left.
                    # This should not occur under normal capacity settings.
                    break
                evicted.append(evicted_meta)

            self._policy.add(key_str, record.hash_ids[pos], timestamp, pos, record.user_id)
            miss_blocks.append(key_str)

        # ---- Phase 3: collect side-channel events ----
        events = self._policy.flush_events()

        return RequestResult(
            record=record,
            hit_block_hashes=hit_blocks,
            miss_block_hashes=miss_blocks,
            evicted_blocks=evicted,
            events=events,
        )
