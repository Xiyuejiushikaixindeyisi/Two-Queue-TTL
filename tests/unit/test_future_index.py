"""Unit tests for sim.analysis.future_index.FutureIndex.

Test categories
---------------
A. Basic shape: empty trace, single record, multi-record.
B. Timestamp-based queries: has_future_access, next_access_time.
C. Request-index-based queries: has_future_access_after_idx, next_access_idx.
D. Prefix-path isolation: only prefix-path keys are indexed, NOT raw hash_ids.
   The critical property: two requests that share hash_ids[i] at position i
   but diverge earlier in the prefix chain must NOT cross-count futures.
E. Same-timestamp tie-breaking: request-index queries distinguish same-T events.
F. Guard: querying before build() raises RuntimeError.
G. Diagnostics: access_count, total_indexed_keys.

Setup helpers
-------------
_make_record(timestamp, model_id, hash_ids) — constructs a minimal TraceRecord.
_keys(model_id, hash_ids) — shortcut for make_prefix_path_keys output.
"""
from __future__ import annotations

import pytest

from sim.analysis.future_index import FutureIndex
from sim.core.prefix_key import make_prefix_path_keys
from sim.core.trace import TraceRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rec(timestamp: float, hash_ids: list[str], model_id: str = "m1") -> TraceRecord:
    return TraceRecord(
        timestamp=timestamp,
        model_id=model_id,
        user_id="u",
        request_type="prefill",
        input_length=len(hash_ids),
        hash_ids=hash_ids,
    )


def _keys(hash_ids: list[str], model_id: str = "m1") -> list[str]:
    return [k.hex() for k in make_prefix_path_keys(model_id, hash_ids)]


# ---------------------------------------------------------------------------
# A. Basic shape
# ---------------------------------------------------------------------------


def test_empty_trace_no_future_access():
    fi = FutureIndex()
    fi.build([])
    key = _keys(["h0"])[0]
    assert fi.has_future_access(key, 0.0) is False
    assert fi.has_future_access_after_idx(key, 0) is False


def test_single_record_no_future_after_its_timestamp():
    """After the only record's timestamp, the key has no future access."""
    trace = [_rec(5.0, ["h0", "h1"])]
    fi = FutureIndex()
    fi.build(trace)

    k0, k1 = _keys(["h0", "h1"])

    # Querying strictly after the record's timestamp → no future.
    assert fi.has_future_access(k0, 5.0) is False
    assert fi.has_future_access(k1, 5.0) is False


def test_single_record_has_future_before_its_timestamp():
    """Before the only record's timestamp, the key has a future access."""
    trace = [_rec(5.0, ["h0", "h1"])]
    fi = FutureIndex()
    fi.build(trace)

    k0, k1 = _keys(["h0", "h1"])

    # Querying before T=5 → the record at T=5 is in the future.
    assert fi.has_future_access(k0, 0.0) is True
    assert fi.has_future_access(k1, 0.0) is True


def test_two_records_second_is_future_of_first():
    """
    R1 at t=1: [h0, h1]
    R2 at t=2: [h0, h1]

    At t=1, both keys have a future access (R2 at t=2).
    After t=2, neither key has a future access.
    """
    trace = [_rec(1.0, ["h0", "h1"]), _rec(2.0, ["h0", "h1"])]
    fi = FutureIndex()
    fi.build(trace)

    k0, k1 = _keys(["h0", "h1"])

    assert fi.has_future_access(k0, 1.0) is True   # R2 at t=2 > t=1
    assert fi.has_future_access(k1, 1.0) is True
    assert fi.has_future_access(k0, 2.0) is False   # nothing after t=2
    assert fi.has_future_access(k1, 2.0) is False


# ---------------------------------------------------------------------------
# B. Timestamp-based queries
# ---------------------------------------------------------------------------


def test_has_future_access_strictly_greater_than():
    """bisect_right → accesses AT after_time are NOT counted as future."""
    trace = [_rec(3.0, ["x"]), _rec(7.0, ["x"])]
    fi = FutureIndex()
    fi.build(trace)

    k = _keys(["x"])[0]
    assert fi.has_future_access(k, 2.9) is True   # both t=3 and t=7 are future
    assert fi.has_future_access(k, 3.0) is True   # t=7 is future; t=3 NOT counted
    assert fi.has_future_access(k, 7.0) is False  # nothing strictly after t=7


def test_next_access_time_returns_correct_timestamp():
    trace = [_rec(1.0, ["h"]), _rec(4.0, ["h"]), _rec(9.0, ["h"])]
    fi = FutureIndex()
    fi.build(trace)

    k = _keys(["h"])[0]
    assert fi.next_access_time(k, 0.0) == 1.0
    assert fi.next_access_time(k, 1.0) == 4.0   # t=1 not counted, next is t=4
    assert fi.next_access_time(k, 4.0) == 9.0
    assert fi.next_access_time(k, 9.0) is None


def test_next_access_time_unknown_key_returns_none():
    trace = [_rec(1.0, ["h0"])]
    fi = FutureIndex()
    fi.build(trace)

    unknown = _keys(["completely_different"])[0]
    assert fi.next_access_time(unknown, 0.0) is None


# ---------------------------------------------------------------------------
# C. Request-index-based queries
# ---------------------------------------------------------------------------


def test_has_future_access_after_idx_basic():
    """
    Trace (3 records, each with key k0):
      idx=0, t=1.0
      idx=1, t=2.0
      idx=2, t=3.0

    After idx=0 → idx=1 and idx=2 are future → True.
    After idx=1 → idx=2 is future → True.
    After idx=2 → nothing → False.
    """
    trace = [_rec(1.0, ["h"]), _rec(2.0, ["h"]), _rec(3.0, ["h"])]
    fi = FutureIndex()
    fi.build(trace)

    k = _keys(["h"])[0]
    assert fi.has_future_access_after_idx(k, 0) is True
    assert fi.has_future_access_after_idx(k, 1) is True
    assert fi.has_future_access_after_idx(k, 2) is False


def test_next_access_idx_returns_correct_index():
    trace = [_rec(1.0, ["h"]), _rec(2.0, ["h"]), _rec(3.0, ["h"])]
    fi = FutureIndex()
    fi.build(trace)

    k = _keys(["h"])[0]
    assert fi.next_access_idx(k, -1) == 0   # before all records
    assert fi.next_access_idx(k, 0) == 1
    assert fi.next_access_idx(k, 1) == 2
    assert fi.next_access_idx(k, 2) is None


def test_same_timestamp_request_index_breaks_tie():
    """
    Two records at the same timestamp t=5.0:
      idx=0, t=5.0: [h0]
      idx=1, t=5.0: [h0]

    has_future_access(k, 5.0) is False (bisect_right skips both t=5.0 entries).
    has_future_access_after_idx(k, 0) is True (idx=1 is strictly after idx=0).
    has_future_access_after_idx(k, 1) is False.

    This shows that index-based queries correctly count idx=1 as future
    of idx=0 even though they share the same timestamp.
    """
    trace = [_rec(5.0, ["h0"]), _rec(5.0, ["h0"])]
    fi = FutureIndex()
    fi.build(trace)

    k = _keys(["h0"])[0]

    # Timestamp query: after t=5.0, both t=5.0 entries are NOT counted.
    assert fi.has_future_access(k, 5.0) is False

    # Index query: idx=1 comes after idx=0.
    assert fi.has_future_access_after_idx(k, 0) is True
    assert fi.has_future_access_after_idx(k, 1) is False


# ---------------------------------------------------------------------------
# D. Prefix-path isolation (the core correctness requirement)
# ---------------------------------------------------------------------------


def test_only_prefix_path_keys_indexed_not_raw_hash_ids():
    """
    R1: model_id=m1, hash_ids=["X", "Z"]   → keys: [K(m1,X), K(m1,X,Z)]
    R2: model_id=m1, hash_ids=["Y", "Z"]   → keys: [K(m1,Y), K(m1,Y,Z)]

    hash_ids[1] = "Z" in both requests.  But the prefix-path keys at position 1
    DIFFER because the chains diverge at position 0.

    Therefore: querying the key from R1-position-1 after t=0 should find R1
    as a future access (t=1), but NOT R2 (t=2), because R2's position-1 key
    is different.  After t=1, R1's position-1 key has no future access.
    """
    r1 = _rec(1.0, ["X", "Z"])
    r2 = _rec(2.0, ["Y", "Z"])
    trace = [r1, r2]

    fi = FutureIndex()
    fi.build(trace)

    # Keys for R1 and R2 at position 1 are DIFFERENT (different prefix chains).
    k_r1_pos1 = _keys(["X", "Z"])[1]  # chain: X → Z
    k_r2_pos1 = _keys(["Y", "Z"])[1]  # chain: Y → Z
    assert k_r1_pos1 != k_r2_pos1, "Sanity: different prefix paths must produce different keys"

    # R1's position-1 key appears at t=1 only (R2 has a different key at pos 1).
    assert fi.has_future_access(k_r1_pos1, 0.0) is True    # R1 at t=1 is future
    assert fi.has_future_access(k_r1_pos1, 1.0) is False   # R2 at t=2 has a DIFFERENT key

    # R2's position-1 key appears at t=2 only.
    assert fi.has_future_access(k_r2_pos1, 0.0) is True    # R2 at t=2 is future
    assert fi.has_future_access(k_r2_pos1, 1.0) is True    # still future
    assert fi.has_future_access(k_r2_pos1, 2.0) is False


def test_shared_prefix_keys_counted_shared_diverged_keys_counted_separately():
    """
    R1: [A, B, C]   → K0=K(A), K1=K(A,B), K2=K(A,B,C)
    R2: [A, B, D]   → K0=K(A), K1=K(A,B), K2=K(A,B,D)  ← diverges at pos 2

    Shared prefix keys K0, K1 appear in BOTH requests → future access visible
    from both timestamps.
    Diverged key K2 from R1 ≠ K2 from R2 → each only has one occurrence.
    """
    r1 = _rec(1.0, ["A", "B", "C"])
    r2 = _rec(3.0, ["A", "B", "D"])
    trace = [r1, r2]

    fi = FutureIndex()
    fi.build(trace)

    k0, k1, k2_r1 = _keys(["A", "B", "C"])
    _, _, k2_r2 = _keys(["A", "B", "D"])

    # Shared keys K0 and K1: appear in R1 (t=1) and R2 (t=3).
    assert fi.has_future_access(k0, 1.0) is True    # R2 at t=3 > t=1
    assert fi.has_future_access(k1, 1.0) is True
    assert fi.has_future_access(k0, 3.0) is False   # nothing after t=3
    assert fi.has_future_access(k1, 3.0) is False

    # Diverged key from R1 only appears in R1 (t=1) → no future after t=1.
    assert fi.has_future_access(k2_r1, 0.0) is True
    assert fi.has_future_access(k2_r1, 1.0) is False   # R2 has DIFFERENT key at pos 2

    # Diverged key from R2 only appears in R2 (t=3).
    assert fi.has_future_access(k2_r2, 1.0) is True
    assert fi.has_future_access(k2_r2, 3.0) is False


def test_same_hash_id_at_position_0_different_models_no_cross_count():
    """
    model_id isolation: blocks from model_A and model_B with the same hash_id
    must NOT be counted as the same key.
    """
    r1 = _rec(1.0, ["h0"], model_id="model_A")
    r2 = _rec(2.0, ["h0"], model_id="model_B")
    trace = [r1, r2]

    fi = FutureIndex()
    fi.build(trace)

    k_A = make_prefix_path_keys("model_A", ["h0"])[0].hex()
    k_B = make_prefix_path_keys("model_B", ["h0"])[0].hex()

    assert k_A != k_B, "Different models must produce different keys"

    # k_A only appears in R1 (t=1); R2 has model_B → different key.
    assert fi.has_future_access(k_A, 1.0) is False
    # k_B only appears in R2 (t=2).
    assert fi.has_future_access(k_B, 1.0) is True
    assert fi.has_future_access(k_B, 2.0) is False


def test_hash_id_at_different_positions_are_different_keys():
    """
    Request [Z, Z]: hash_ids[0] == hash_ids[1] == "Z".
    The key at position 0 and position 1 must be DIFFERENT (different chain states).
    The FutureIndex tracks them independently.
    """
    trace = [_rec(1.0, ["Z", "Z"])]
    fi = FutureIndex()
    fi.build(trace)

    k_pos0, k_pos1 = _keys(["Z", "Z"])
    assert k_pos0 != k_pos1, "Same hash_id at different positions must differ"

    # Both appear exactly once (at t=1).
    assert fi.access_count(k_pos0) == 1
    assert fi.access_count(k_pos1) == 1
    assert fi.has_future_access(k_pos0, 1.0) is False
    assert fi.has_future_access(k_pos1, 1.0) is False


# ---------------------------------------------------------------------------
# E. access_count and total_indexed_keys
# ---------------------------------------------------------------------------


def test_access_count_counts_all_occurrences():
    """
    [h0, h1] appears in 3 requests.
    Key at position 0 (m1:h0 chain) has access_count == 3.
    """
    trace = [_rec(1.0, ["h0", "h1"]), _rec(2.0, ["h0", "h1"]), _rec(3.0, ["h0", "h1"])]
    fi = FutureIndex()
    fi.build(trace)

    k0 = _keys(["h0", "h1"])[0]
    assert fi.access_count(k0) == 3


def test_access_count_zero_for_unknown_key():
    fi = FutureIndex()
    fi.build([])
    k = _keys(["nonexistent"])[0]
    assert fi.access_count(k) == 0


def test_total_indexed_keys_counts_distinct_prefix_path_keys():
    """
    R1: [A, B]   → 2 distinct keys
    R2: [A, C]   → K(A) already seen + 1 new = 3 total distinct keys
    R3: [A, B]   → both K(A) and K(A,B) already seen; no new keys

    total_indexed_keys == 3: K(A), K(A,B), K(A,C)
    """
    trace = [_rec(1.0, ["A", "B"]), _rec(2.0, ["A", "C"]), _rec(3.0, ["A", "B"])]
    fi = FutureIndex()
    fi.build(trace)

    assert fi.total_indexed_keys() == 3


# ---------------------------------------------------------------------------
# F. Guard: querying before build()
# ---------------------------------------------------------------------------


def test_query_before_build_raises():
    fi = FutureIndex()
    k = _keys(["h0"])[0]
    with pytest.raises(RuntimeError, match="build()"):
        fi.has_future_access(k, 0.0)


def test_index_based_query_before_build_raises():
    fi = FutureIndex()
    k = _keys(["h0"])[0]
    with pytest.raises(RuntimeError, match="build()"):
        fi.has_future_access_after_idx(k, 0)


# ---------------------------------------------------------------------------
# G. Integration with sample trace (realistic data)
# ---------------------------------------------------------------------------


def test_build_from_real_sample_trace():
    """Smoke test: build from the sample fixture and do basic sanity checks."""
    from sim.io.trace_loader import load_trace
    import os

    fixture_path = os.path.join(
        os.path.dirname(__file__), "..", "fixtures", "sample_trace.csv"
    )
    trace = load_trace(fixture_path)
    fi = FutureIndex()
    fi.build(trace)

    # total_indexed_keys > 0 (non-empty trace).
    assert fi.total_indexed_keys() > 0

    # The trace has 10 records; all keys should have at least 1 occurrence.
    # sys_a appears in many requests; its prefix-path key at position 0
    # should appear multiple times.
    k_sys_a = make_prefix_path_keys("qwen-v3", ["sys_a"])[0].hex()
    assert fi.access_count(k_sys_a) >= 2

    # After the last timestamp (t=1060), no future accesses.
    assert fi.has_future_access(k_sys_a, 1060.0) is False

    # Before the last timestamp, there should be future accesses.
    assert fi.has_future_access(k_sys_a, 1000.0) is True
