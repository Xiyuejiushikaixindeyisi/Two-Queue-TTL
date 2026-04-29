"""FutureIndex: precomputed lookup for whether a prefix-path key will be
accessed again in the future.

Role in the two-phase metrics architecture
-------------------------------------------
Phase 1 (build, before replay):
    FutureIndex.build(trace) expands every TraceRecord into prefix-path keys
    via make_prefix_path_keys and records each key's occurrence timestamps and
    request indices.  This is done ONCE before the replay loop starts.

Phase 2 (replay):
    The simulation engine runs normally; eviction events are accumulated.

Phase 3 (finalize, after replay):
    MetricsCollector.finalize() queries the FutureIndex for each recorded
    eviction: "did this key appear again after the eviction?"
    This answers evicted_before_next_hit_count and protected_pollution_count
    without any lookahead during the replay itself.

Key-type contract
-----------------
FutureIndex uses hex-string keys produced by make_prefix_path_keys().hex().
All keys are str throughout: build(), queries, and internal storage.
This matches the key type used by PrefixCache, MetricsCollector, and policies
— no bytes/str mixing anywhere in the pipeline.

It does NOT index individual hash_ids.  Indexing raw hash_ids would be
wrong: two requests that share hash_ids[i] at position i but diverge
earlier in the prefix chain have DIFFERENT KV tensors at position i and
must NOT be treated as the same cached block.

Specifically, request [h0, Z] and request [X, Z] produce different keys at
position 1 (keys[1] encodes the full chain, so they differ).  FutureIndex
correctly treats them as separate entries.

Query semantics
---------------
Two complementary query axes are supported:

  has_future_access(key, after_time):
      True if the key appears in any request with timestamp > after_time.
      Uses bisect_right on a sorted timestamp list → O(log M).
      Correct when eviction timestamps are unique or when same-timestamp
      collisions are acceptable.

  has_future_access_after_idx(key, after_req_idx):
      True if the key appears in any request with index > after_req_idx.
      Uses bisect_right on a sorted request-index list → O(log M).
      More precise when multiple requests share the same timestamp: the
      eviction at request R should not count request R itself as a
      "future" occurrence, even if another request at the same timestamp
      T follows R in replay order.

Callers should prefer has_future_access_after_idx when the request index
at eviction time is known.  has_future_access is a convenience wrapper for
cases where only a timestamp is available.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from typing import Dict, List, Optional

from ..core.prefix_key import make_prefix_path_keys
from ..core.trace import TraceRecord


class FutureIndex:
    """Precomputed prefix-path future-access lookup table.

    Call build() once before the replay loop.  Then query via
    has_future_access() or has_future_access_after_idx() during finalize().

    Internal representation
    -----------------------
    _ts_index  : Dict[str, List[float]] — sorted timestamps per key (hex str).
    _req_index : Dict[str, List[int]]  — sorted request indices per key (hex str).

    Both lists are appended to in trace order (which is timestamp-sorted by
    load_trace), so they are already sorted after build() without an
    explicit sort step.
    """

    def __init__(self) -> None:
        self._ts_index: Dict[str, List[float]] = {}
        self._req_index: Dict[str, List[int]] = {}
        self._built = False

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, trace: List[TraceRecord]) -> None:
        """Precompute future-access data from the full trace.

        Time complexity: O(N × L) where N = len(trace) and L is the
        average number of blocks (hash_ids) per request.
        Space complexity: O(N × L) entries total across all keys.

        The trace must be sorted by timestamp before calling build().
        load_trace() guarantees this; if you construct records manually,
        sort them first.

        Parameters
        ----------
        trace:
            Full list of TraceRecords in timestamp-ascending order.
        """
        ts_acc: Dict[str, List[float]] = defaultdict(list)
        req_acc: Dict[str, List[int]] = defaultdict(list)

        for req_idx, record in enumerate(trace):
            keys = make_prefix_path_keys(record.model_id, record.hash_ids)
            ts = record.timestamp
            for key in keys:
                key_str = key.hex()
                ts_acc[key_str].append(ts)
                req_acc[key_str].append(req_idx)

        # Freeze to plain dicts; defaultdict overhead not needed after build.
        self._ts_index = dict(ts_acc)
        self._req_index = dict(req_acc)
        self._built = True

    # ------------------------------------------------------------------
    # Timestamp-based query
    # ------------------------------------------------------------------

    def has_future_access(self, key: str, after_time: float) -> bool:
        """Return True if key appears in any request with timestamp > after_time.

        Uses bisect_right so that requests at exactly after_time are NOT
        counted (the eviction happens at after_time, so occurrences at the
        same timestamp are concurrent, not strictly future).

        O(log M) where M is the number of occurrences of key in the trace.
        """
        self._check_built()
        timestamps = self._ts_index.get(key)
        if not timestamps:
            return False
        return bisect_right(timestamps, after_time) < len(timestamps)

    def next_access_time(self, key: str, after_time: float) -> Optional[float]:
        """Return the timestamp of the first access strictly after after_time.

        Returns None if no such access exists.
        O(log M).
        """
        self._check_built()
        timestamps = self._ts_index.get(key)
        if not timestamps:
            return None
        idx = bisect_right(timestamps, after_time)
        return timestamps[idx] if idx < len(timestamps) else None

    # ------------------------------------------------------------------
    # Request-index-based query (tie-breaking for same-timestamp requests)
    # ------------------------------------------------------------------

    def has_future_access_after_idx(self, key: str, after_req_idx: int) -> bool:
        """Return True if key appears in any request with index > after_req_idx.

        More precise than has_future_access when multiple requests share the
        same timestamp: the eviction occurs during request after_req_idx, so
        only requests with a strictly greater index are "future".

        O(log M).
        """
        self._check_built()
        indices = self._req_index.get(key)
        if not indices:
            return False
        return bisect_right(indices, after_req_idx) < len(indices)

    def next_access_idx(self, key: str, after_req_idx: int) -> Optional[int]:
        """Return the request index of the first access after after_req_idx.

        Returns None if no such access exists.
        O(log M).
        """
        self._check_built()
        indices = self._req_index.get(key)
        if not indices:
            return None
        idx = bisect_right(indices, after_req_idx)
        return indices[idx] if idx < len(indices) else None

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def access_count(self, key: str) -> int:
        """Total number of times key appears in the trace (all positions)."""
        self._check_built()
        return len(self._ts_index.get(key, []))

    def total_indexed_keys(self) -> int:
        """Number of distinct prefix-path keys in the index."""
        self._check_built()
        return len(self._ts_index)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _check_built(self) -> None:
        if not self._built:
            raise RuntimeError("FutureIndex.build() must be called before querying.")
