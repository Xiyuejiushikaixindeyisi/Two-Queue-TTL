from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class TraceRecord:
    """One request event from the trace.

    Fields
    ------
    timestamp:
        Unix seconds.  Used for TTL calculation and replay ordering.
    model_id:
        Model identifier.  Allows multi-model trace files.
    user_id:
        User / tenant identifier.  Used for shared_score and routing experiments.
    request_type:
        Workload scenario label (e.g. "chat", "agent", "doc").
        Optional; used for per-scenario metric breakdowns.
    input_length:
        Total input token count.  Used to compute saved_prefill_tokens.
    hash_ids:
        Ordered list of block hashes covering the full input prefix chain.
        Position i in the list corresponds to block_pos i in the cache.
    output_length:
        Output token count.  Not used by Phase-1 simulation; kept for completeness.
    turn:
        Conversation turn index within a session.  Optional.
    """

    timestamp: float
    model_id: str
    user_id: str
    request_type: str
    input_length: int
    hash_ids: List[str]
    output_length: int = 0
    turn: int = 0
