"""Shared future-access index used by Belady Oracle and MetricsCollector.

The index maps prefix-path block key (hex) → sorted list of all access
timestamps in the trace.  Two callers share this computation:

  * BeladyOraclePolicy  — needs next_access_after() at eviction time.
  * MetricsCollector    — needs has_future_access() for evicted_before_next_hit.
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from typing import Dict, List

from .prefix_key import make_prefix_path_keys
from .trace import TraceRecord

FutureIndex = Dict[str, List[float]]


def build_future_index(trace: List[TraceRecord]) -> FutureIndex:
    """Return block_key_hex → sorted access timestamps for the whole trace."""
    index: Dict[str, List[float]] = defaultdict(list)
    for record in trace:
        keys = make_prefix_path_keys(record.model_id, record.hash_ids)
        for key in keys:
            index[key.hex()].append(record.timestamp)
    return {k: sorted(v) for k, v in index.items()}


def next_access_after(index: FutureIndex, block_key: str, after_time: float) -> float:
    """Return the next access timestamp for block_key strictly after after_time.

    Returns float('inf') if the block has no future access (ideal eviction
    candidate for Belady).
    """
    times = index.get(block_key)
    if not times:
        return float("inf")
    idx = bisect.bisect_right(times, after_time)
    return times[idx] if idx < len(times) else float("inf")


def has_future_access(index: FutureIndex, block_key: str, after_time: float) -> bool:
    """Return True if block_key is accessed at any timestamp > after_time."""
    times = index.get(block_key)
    if not times:
        return False
    return bisect.bisect_right(times, after_time) < len(times)
