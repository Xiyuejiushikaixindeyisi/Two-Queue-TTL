#!/usr/bin/env python3
"""Unified trace converter — replaces convert_raw_trace.py.

Two modes:

  --mode raw
      Read a CSV with a `raw_prompt` text column (or `请求参数` alias).
      Apply chat_template (wrap_user by default), tokenize with the vendored
      GLM-5 tokenizer, chunk into 128-token blocks, SHA-256 chain → 1 hash_ids
      column. Output schema: timestamp, model_id, user_id, request_type,
      input_length, hash_ids.

  --mode chat
      Read a CSV with a `request_input` column containing the full chat
      completions request body (JSON). For each row, parse tools + messages,
      apply the 4 transform variants (base / reorder / placeholder / both),
      render with apply_chat_template(messages, tools), tokenize each variant,
      SHA-256 chain → 4 hash_ids columns. Output schema adds
      hash_ids_base, hash_ids_reorder, hash_ids_placeholder, hash_ids_both.

Both modes use lib.prompt_encoder / lib.hf_tokenizer (the Step 1.6 codepath
that aligns with vllm-ascend), fixing the old convert_raw_trace.py issue of
not applying the chat template.

Example
-------
    # Replace old convert_raw_trace.py call
    python scripts/convert_trace.py --mode raw \\
        --input  data/<app>/raw/<file>.csv \\
        --output data/<app>/raw/<file>_converted.csv \\
        --chat-mode wrap_user

    # New chat-trace path (4 variants in one pass)
    python scripts/convert_trace.py --mode chat \\
        --input  data/<app>_raw.csv \\
        --output data/<app>_4variant.csv \\
        --patterns configs/dynamic_patterns.json   # optional
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Raw prompts from 64K-context models can exceed the default field-size limit.
csv.field_size_limit(10 * 1024 * 1024)  # 10 MB — covers 64K tokens comfortably


# ---------------------------------------------------------------------------
# SHA-256 chain hash (matches lib.prompt_encoder.HFTokenEncoder)
# ---------------------------------------------------------------------------

_HASH_HEX_LEN = 16  # 64-bit collision space; matches the legacy CSV cell width


def _sha256_chain(token_ids: list[int], block_size: int) -> list[str]:
    """Chain SHA-256 over fixed-size token blocks, return hex-truncated keys.

    Matches `lib.prompt_encoder.HFTokenEncoder.encode()` exactly:
      K[0] = sha256("" || ",".join(str(t) for t in tokens[0:bs]))
      K[i] = sha256(K[i-1] || ",".join(str(t) for t in tokens[i*bs:(i+1)*bs]))
    Then hex-encode and truncate each to _HASH_HEX_LEN (16) chars to keep CSV
    cells compact.
    """
    out: list[str] = []
    prev = b""
    for i in range(0, len(token_ids), block_size):
        block = token_ids[i:i + block_size]
        payload = ",".join(str(t) for t in block).encode("utf-8")
        h = hashlib.sha256()
        h.update(prev)
        h.update(payload)
        prev = h.digest()
        out.append(prev.hex()[:_HASH_HEX_LEN])
    return out


# ---------------------------------------------------------------------------
# Column-name aliases (raw mode)
# ---------------------------------------------------------------------------

_RAW_COL_ALIASES = {
    "请求ID":   "request_id",
    "租户ID":   "user_id",
    "请求参数": "raw_prompt",
}

_CHAT_USER_ALIASES = ("user_id", "租户ID")
_CHAT_TS_ALIASES = ("timestamp", "create_time", "ts", "time")


def _normalize_raw_row(row: dict) -> dict:
    return {_RAW_COL_ALIASES.get(k, k): v for k, v in row.items()}


def _first_present(row: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        if k in row and row[k]:
            return row[k]
    return ""


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _write_csv(path: str, records: list[dict], columns: list[str]) -> None:
    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fout:
        writer = csv.DictWriter(fout, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)


def _print_dry_preview(records: list[dict], hash_cols: list[str]) -> None:
    print(f"── Dry run: preview {min(5, len(records))} of {len(records):,} rows ──")
    for r in records[:5]:
        first = r[hash_cols[0]]
        blocks = first.split("|") if first else []
        cols_summary = "  ".join(
            f"{c}={len((r[c] or '').split('|'))}b" for c in hash_cols
        )
        print(f"  ts={r.get('timestamp', '')}  user={r.get('user_id', '')}  "
              f"{cols_summary}  first_hash={blocks[0] if blocks else '<empty>'}")


# ---------------------------------------------------------------------------
# Raw mode
# ---------------------------------------------------------------------------

def run_raw_mode(args, tokenizer) -> None:
    from lib.hf_tokenizer import apply_template

    with open(args.input, newline="", encoding="utf-8-sig") as fin:
        reader = csv.DictReader(fin)
        raw_rows = list(reader)

    if not raw_rows:
        print("Input is empty.")
        return

    rows = [_normalize_raw_row(r) for r in raw_rows]
    out_records: list[dict] = []
    skipped_empty = 0

    for i, row in enumerate(rows, 1):
        prompt = row.get("raw_prompt", "") or ""
        if not prompt:
            skipped_empty += 1
            continue

        token_ids = apply_template(tokenizer, prompt, args.chat_mode)
        if not token_ids:
            skipped_empty += 1
            continue

        hash_ids = _sha256_chain(token_ids, args.block_size)
        out_records.append({
            "timestamp":    row.get("timestamp", ""),
            "model_id":     args.model_id,
            "user_id":      row.get("user_id", ""),
            "request_type": args.request_type,
            "input_length": len(token_ids),
            "hash_ids":     "|".join(hash_ids),
        })

        if i % 1000 == 0:
            print(f"  processed {i:,}/{len(rows):,} rows ...", flush=True)

    print(f"Converted: {len(out_records):,} rows; skipped empty: {skipped_empty:,}")

    cols = ["timestamp", "model_id", "user_id", "request_type",
            "input_length", "hash_ids"]

    if args.dry_run:
        _print_dry_preview(out_records, ["hash_ids"])
        return

    _write_csv(args.output, out_records, cols)
    print(f"Wrote → {args.output}")


# ---------------------------------------------------------------------------
# Chat mode (4 variants per row)
# ---------------------------------------------------------------------------

def run_chat_mode(args, tokenizer) -> None:
    from lib.prompt_rewrite import (
        DEFAULT_PATTERNS,
        apply_variant,
        load_patterns_config,
    )
    from lib.prompt_rewrite.chat_render import render_to_tokens
    from lib.prompt_rewrite.detect import VARIANTS

    patterns = (
        load_patterns_config(args.patterns) if args.patterns else DEFAULT_PATTERNS
    )

    if args.variants:
        variants = tuple(v.strip() for v in args.variants.split(","))
    else:
        variants = VARIANTS
    for v in variants:
        if v not in VARIANTS:
            raise ValueError(f"unknown variant {v!r}; must be one of {VARIANTS}")

    with open(args.input, newline="", encoding="utf-8-sig") as fin:
        reader = csv.DictReader(fin)
        raw_rows = list(reader)

    if not raw_rows:
        print("Input is empty.")
        return

    out_records: list[dict] = []
    skipped_no_json = 0
    skipped_no_messages = 0

    for i, row in enumerate(raw_rows, 1):
        ri = row.get("request_input", "") or ""
        if not ri:
            skipped_no_json += 1
            continue
        try:
            body = json.loads(ri)
        except (json.JSONDecodeError, TypeError):
            skipped_no_json += 1
            continue

        tools = body.get("tools") or []
        messages = body.get("messages") or []
        if not messages:
            skipped_no_messages += 1
            continue

        record = {
            "timestamp":    _first_present(row, _CHAT_TS_ALIASES),
            "model_id":     args.model_id,
            "user_id":      _first_present(row, _CHAT_USER_ALIASES),
            "request_type": args.request_type,
        }

        base_token_count = 0
        for v in variants:
            transformed_tools = apply_variant(v, tools, patterns=patterns)
            token_ids = render_to_tokens(tokenizer, messages, transformed_tools)
            hash_ids = _sha256_chain(token_ids, args.block_size)
            record[f"hash_ids_{v}"] = "|".join(hash_ids)
            if v == "base":
                base_token_count = len(token_ids)

        record["input_length"] = base_token_count
        out_records.append(record)

        if i % 100 == 0:
            print(f"  processed {i:,}/{len(raw_rows):,} rows ...", flush=True)

    print(f"Converted: {len(out_records):,} rows; "
          f"skipped no-json: {skipped_no_json:,}; "
          f"skipped no-messages: {skipped_no_messages:,}")

    cols = ["timestamp", "model_id", "user_id", "request_type", "input_length"]
    hash_cols = [f"hash_ids_{v}" for v in variants]
    cols.extend(hash_cols)

    if args.dry_run:
        _print_dry_preview(out_records, hash_cols)
        return

    _write_csv(args.output, out_records, cols)
    print(f"Wrote → {args.output}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--mode", choices=["raw", "chat"], required=True,
                   help="raw: read raw_prompt text column; "
                        "chat: read request_input JSON column with tools+messages")
    p.add_argument("--input", required=True, help="Input CSV path")
    p.add_argument("--output", default=None,
                   help="Output CSV path (default: <input>_converted.csv or _4variant.csv)")
    p.add_argument("--tokenizer", default="models/glm5_tokenizer",
                   help="Path to HF tokenizer directory")
    p.add_argument("--block-size", type=int, default=128,
                   help="Tokens per cache block (vllm-ascend uses 128)")
    p.add_argument("--model-id", default="default",
                   help="model_id constant written to every output row")
    p.add_argument("--request-type", default="chat",
                   help="request_type constant for every output row")
    p.add_argument("--dry-run", action="store_true",
                   help="Print preview of first 5 converted rows and exit")

    # raw-mode only
    raw_group = p.add_argument_group("--mode raw options")
    raw_group.add_argument("--chat-mode", default="wrap_user",
                           choices=["raw", "wrap_user", "messages"],
                           help="raw mode: how to wrap the prompt text "
                                "(default wrap_user applies the model's chat template)")

    # chat-mode only
    chat_group = p.add_argument_group("--mode chat options")
    chat_group.add_argument("--patterns", default=None,
                            help="chat mode: JSON config overriding dynamic patterns")
    chat_group.add_argument("--variants", default=None,
                            help="chat mode: comma-separated subset of 4 variants "
                                 "(default: base,reorder,placeholder,both)")

    args = p.parse_args()

    if args.output is None:
        base, _ext = os.path.splitext(args.input)
        suffix = "_4variant" if args.mode == "chat" else "_converted"
        args.output = base + suffix + ".csv"

    return args


def main() -> None:
    args = parse_args()

    from lib.hf_tokenizer import load_tokenizer
    print(f"Loading tokenizer from: {args.tokenizer}")
    tokenizer = load_tokenizer(args.tokenizer)

    print(f"Mode              : {args.mode}")
    print(f"Block size        : {args.block_size}")
    print(f"Input             : {args.input}")
    if not args.dry_run:
        print(f"Output            : {args.output}")
    if args.mode == "raw":
        print(f"Chat mode         : {args.chat_mode}")
    else:
        print(f"Variants          : {args.variants or 'base,reorder,placeholder,both'}")
        if args.patterns:
            print(f"Patterns config   : {args.patterns}")
    print()

    if args.mode == "raw":
        run_raw_mode(args, tokenizer)
    else:
        run_chat_mode(args, tokenizer)


if __name__ == "__main__":
    main()
