"""Load request traces from CSV or JSONL files.

Expected CSV columns
--------------------
timestamp, model_id, user_id, request_type, input_length, hash_ids
[output_length, turn]   ← optional

hash_ids column format: pipe-separated block hashes, e.g.
    "a1b2c3|d4e5f6|g7h8i9"

Expected JSONL format
---------------------
One JSON object per line with the same field names as the CSV.
hash_ids should be a JSON array of strings.
"""
from __future__ import annotations

import csv
import json
import os
import time
from typing import List

from ..core.prefix_key import make_prefix_path_keys
from ..core.trace import TraceRecord


def load_trace(path: str) -> List[TraceRecord]:
    """Load and sort a trace file by timestamp.

    Supports .csv and .jsonl/.ndjson.
    Returns records sorted ascending by timestamp.

    Note: prefix_path_keys (SHA-256 chain) are NOT precomputed here.
    Call precompute_prefix_keys(records) before running simulations to avoid
    recomputing millions of SHA-256 operations per simulation run.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        records = _load_csv(path)
    elif ext in (".jsonl", ".ndjson", ".json"):
        records = _load_jsonl(path)
    else:
        raise ValueError(f"Unsupported trace format: {ext!r}. Use .csv or .jsonl")

    records.sort(key=lambda r: r.timestamp)
    return records


def precompute_prefix_keys(records: List[TraceRecord], report_every: int = 1000) -> None:
    """Precompute and cache SHA-256 prefix-path keys for every record in-place.

    This is a one-time O(total_block_refs) computation.  Call it once after
    load_trace() and before the first SimulationRunner.compare() call.
    All subsequent simulation runs reuse the cached keys with zero SHA-256 cost.

    Parameters
    ----------
    records:
        Loaded trace records (mutated in place: prefix_path_keys is set).
    report_every:
        Print progress every N records.  Set to 0 to suppress output.
    """
    n = len(records)
    if n == 0:
        return
    total_blocks = sum(len(r.hash_ids) for r in records)
    print(
        f"  Precomputing prefix-path keys: {n:,} records, "
        f"{total_blocks:,} total blocks …",
        flush=True,
    )
    t0 = time.time()
    for i, record in enumerate(records, 1):
        record.prefix_path_keys = make_prefix_path_keys(record.model_id, record.hash_ids)
        if report_every and i % report_every == 0:
            print(f"    {i:,}/{n:,} records done …", flush=True)
    elapsed = time.time() - t0
    print(f"  Prefix keys ready  ({elapsed:.1f}s)", flush=True)


def _load_csv(path: str) -> List[TraceRecord]:
    records: List[TraceRecord] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append(_row_to_record(row))
    return records


def _load_jsonl(path: str) -> List[TraceRecord]:
    records: List[TraceRecord] = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON — {exc}") from exc
            records.append(_obj_to_record(obj))
    return records


def _row_to_record(row: dict) -> TraceRecord:
    raw_hashes = row["hash_ids"].strip()
    if raw_hashes.startswith("["):
        hash_ids = json.loads(raw_hashes)
    else:
        hash_ids = [h.strip() for h in raw_hashes.split("|") if h.strip()]

    return TraceRecord(
        timestamp=float(row["timestamp"]),
        model_id=row["model_id"].strip(),
        user_id=row["user_id"].strip(),
        request_type=row.get("request_type", "unknown").strip(),
        input_length=int(row["input_length"]),
        hash_ids=hash_ids,
        output_length=int(row.get("output_length", 0) or 0),
        turn=int(row.get("turn", 0) or 0),
    )


def _obj_to_record(obj: dict) -> TraceRecord:
    hash_ids = obj["hash_ids"]
    if isinstance(hash_ids, str):
        if hash_ids.startswith("["):
            hash_ids = json.loads(hash_ids)
        else:
            hash_ids = [h.strip() for h in hash_ids.split("|") if h.strip()]

    return TraceRecord(
        timestamp=float(obj["timestamp"]),
        model_id=str(obj["model_id"]),
        user_id=str(obj["user_id"]),
        request_type=str(obj.get("request_type", "unknown")),
        input_length=int(obj["input_length"]),
        hash_ids=hash_ids,
        output_length=int(obj.get("output_length", 0) or 0),
        turn=int(obj.get("turn", 0) or 0),
    )
