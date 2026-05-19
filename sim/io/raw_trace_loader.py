"""Load raw-prompt CSV traces into TraceRecord lists.

Expected input CSV schema
-------------------------
request_id  : str   — unique request identifier (used for dedup/logging only)
user_id     : str   — user / session identifier
raw_prompt  : str   — full input text; tokenized here into block hash_ids
timestamp   : float — Unix timestamp (seconds) when the request arrived

All other fields needed by TraceRecord are filled with sensible constants:
  model_id      = <configurable, default "default">
  request_type  = "prefill"
  input_length  = actual token count (computed from tokenizer)
  output_length = 0
  turn          = 0

Usage
-----
    from sim.io.raw_trace_loader import load_raw_trace
    trace = load_raw_trace("data/my_trace.csv", block_size=128)

For large files the tokenization step dominates.  Progress is printed every
1000 rows so you can estimate wall time.
"""
from __future__ import annotations

import csv
from typing import List

from ..core.trace import TraceRecord
from .prompt_tokenizer import compute_hash_ids, tokenizer_backend


def load_raw_trace(
    path: str,
    block_size: int = 128,
    model_id: str = "default",
    request_type: str = "prefill",
    verbose: bool = True,
) -> List[TraceRecord]:
    """Load a raw-prompt CSV and return a timestamp-sorted list of TraceRecords.

    Parameters
    ----------
    path:
        Path to the CSV file.
    block_size:
        Tokens per cache block.  Must match SimConfig.block_size.
    model_id:
        Fixed model identifier inserted into the prefix-path key formula.
        Use any consistent string when all rows come from the same model.
    request_type:
        Fixed request type label (e.g. "prefill", "agent").
    verbose:
        Print tokenization progress every 1000 rows.
    """
    if verbose:
        print(f"[raw_trace_loader] backend = {tokenizer_backend()}, block_size = {block_size}")

    records: List[TraceRecord] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        _check_columns(reader.fieldnames or [], path)

        for i, row in enumerate(reader, 1):
            prompt = row["raw_prompt"]
            hash_ids = compute_hash_ids(prompt, block_size)
            n_tokens = len(hash_ids) * block_size  # approximate

            records.append(TraceRecord(
                timestamp=float(row["timestamp"]),
                model_id=model_id,
                user_id=row["user_id"],
                request_type=request_type,
                input_length=n_tokens,
                hash_ids=hash_ids,
                output_length=0,
                turn=0,
            ))

            if verbose and i % 1000 == 0:
                print(f"  tokenized {i:,} rows …", flush=True)

    if verbose:
        print(f"  done: {len(records):,} records loaded")

    records.sort(key=lambda r: r.timestamp)
    return records


def _check_columns(fieldnames: List[str], path: str) -> None:
    required = {"request_id", "user_id", "raw_prompt", "timestamp"}
    missing = required - set(fieldnames)
    if missing:
        raise ValueError(
            f"{path}: missing required columns {sorted(missing)}. "
            f"Found: {fieldnames}"
        )
