#!/usr/bin/env python3
"""Convert a raw-prompt CSV to the standard hash_ids CSV format.

The converted file can be used with all existing CLI tools
(run_simulation.py, run_phase1.py, analyze_capacity_sweep.py).

Input CSV columns (required)
-----------------------------
    request_id, user_id, raw_prompt, timestamp

Output CSV columns
------------------
    timestamp, model_id, user_id, request_type, input_length, hash_ids

Usage
-----
    python scripts/convert_raw_trace.py \\
        --input data/my_raw_trace.csv \\
        --output data/my_trace.csv \\
        --block-size 128

    # Preview: tokenizer backend and first 5 rows, no file written
    python scripts/convert_raw_trace.py \\
        --input data/my_raw_trace.csv \\
        --dry-run
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Raw prompts from 64K-context models can exceed the default 131072-char limit.
csv.field_size_limit(10 * 1024 * 1024)  # 10 MB — comfortably covers 64K tokens

from sim.io.prompt_tokenizer import compute_hash_ids, tokenizer_backend


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Convert raw-prompt CSV to standard hash_ids CSV",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--input",  required=True, help="Input CSV path (raw_prompt format)")
    p.add_argument("--output", default=None,  help="Output CSV path (default: <input>_converted.csv)")
    p.add_argument("--block-size", type=int, default=128, help="Tokens per cache block")
    p.add_argument("--model-id",   default="default",    help="model_id constant for all rows")
    p.add_argument("--request-type", default="prefill",  help="request_type constant for all rows")
    p.add_argument("--dry-run", action="store_true",
                   help="Print first 5 converted rows and exit without writing")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if args.output is None:
        base, ext = os.path.splitext(args.input)
        args.output = base + "_converted.csv"

    print(f"Tokenizer backend : {tokenizer_backend()}")
    print(f"Block size        : {args.block_size} tokens")
    print(f"Input             : {args.input}")
    if not args.dry_run:
        print(f"Output            : {args.output}")
    print()

    out_fields = ["timestamp", "model_id", "user_id", "request_type",
                  "input_length", "hash_ids"]

    # Column name aliases: maps alternative names → canonical names used below.
    # Add entries here if the source CSV uses different column names.
    _COL_ALIASES = {
        "请求ID":   "request_id",
        "租户ID":   "user_id",
        "请求参数": "raw_prompt",
    }

    with open(args.input, newline="", encoding="utf-8") as fin:
        reader = csv.DictReader(fin)
        raw_rows = list(reader)

    if not raw_rows:
        print("Input file is empty.")
        return

    # Normalise column names using alias table.
    def _normalise(row: dict) -> dict:
        return {_COL_ALIASES.get(k, k): v for k, v in row.items()}

    rows = [_normalise(r) for r in raw_rows]

    converted = []
    for i, row in enumerate(rows, 1):
        prompt = row["raw_prompt"]
        hash_ids = compute_hash_ids(prompt, args.block_size)
        converted.append({
            "timestamp":    row["timestamp"],
            "model_id":     args.model_id,
            "user_id":      row["user_id"],
            "request_type": args.request_type,
            "input_length": len(hash_ids) * args.block_size,
            "hash_ids":     "|".join(hash_ids),
        })
        if i % 1000 == 0:
            print(f"  processed {i:,} / {len(rows):,} rows …", flush=True)

    if args.dry_run:
        print("── First 5 converted rows ──────────────────────────")
        for row in converted[:5]:
            hids = row["hash_ids"].split("|")
            print(f"  ts={row['timestamp']}  user={row['user_id']}  "
                  f"blocks={len(hids)}  hash_ids[0]={hids[0]}")
        return

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as fout:
        writer = csv.DictWriter(fout, fieldnames=out_fields)
        writer.writeheader()
        writer.writerows(converted)

    print(f"Converted {len(converted):,} rows → {args.output}")


if __name__ == "__main__":
    main()
