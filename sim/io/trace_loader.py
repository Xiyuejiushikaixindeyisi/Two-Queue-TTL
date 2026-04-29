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
from typing import List

from ..core.trace import TraceRecord


def load_trace(path: str) -> List[TraceRecord]:
    """Load and sort a trace file by timestamp.

    Supports .csv and .jsonl/.ndjson.
    Returns records sorted ascending by timestamp.
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
