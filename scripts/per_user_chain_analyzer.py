#!/usr/bin/env python3
"""Step 1.2 — Per-user prefix-path chain detection.

For each user_id (实为 product_id), build an independent trie and find the
LCP using the same algorithm as Step 1.1. Outputs:
  - Each user's chain (length, coverage, decoded content, branch points)
  - Comparison with the global (cross-user) chain (prefix match, identical?)
  - Aggregate statistics across users

This script reuses Step 1.1 primitives directly (trie, hashing, LCP finder,
decoder), keeping the algorithm definition in one place.

Reusable across models. Same input contract as Step 1.1 (4-column raw CSV).
Tokenizer-free; offline-safe.

Usage
-----
  python scripts/per_user_chain_analyzer.py \
      --raw-csv data/dsk8k_2h_5k/raw \
      --output  outputs/dsk8k_2h_5k/per_user_chains.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

# Reuse Step 1.1 primitives
sys.path.insert(0, str(Path(__file__).parent))
from verify_chain_path_closure import (  # noqa: E402
    TrieNode,
    _canon_fieldnames,
    compute_prefix_path_keys,
    discover_csv_files,
    find_lcp,
    find_sample_request_at_depth,
    iter_raw_records,
    split_blocks,
    trie_insert,
)

csv.field_size_limit(sys.maxsize)


# ---------------------------------------------------------------------------
# Batched decoding
# ---------------------------------------------------------------------------

def batch_decode(
    csv_files: list[Path],
    needed: dict[str, int],   # request_id -> max_blocks_to_decode
    block_size: int,
) -> dict[str, list[str]]:
    """Single-pass over CSV files to decode multiple chains efficiently."""
    out: dict[str, list[str]] = {}
    if not needed:
        return out
    remaining = set(needed)
    for csv_path in csv_files:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            mapping = _canon_fieldnames(reader.fieldnames or [])
            rid_col = mapping.get("request_id")
            prompt_col = mapping.get("raw_prompt")
            if not rid_col or not prompt_col:
                continue
            for row in reader:
                rid = row.get(rid_col)
                if rid in remaining:
                    n = needed[rid]
                    blocks = split_blocks(row[prompt_col], block_size)
                    out[rid] = [
                        b.decode("utf-8", errors="replace") for b in blocks[:n]
                    ]
                    remaining.discard(rid)
                    if not remaining:
                        return out
    return out


# ---------------------------------------------------------------------------
# Chain comparison
# ---------------------------------------------------------------------------

def common_prefix_length(a: list[bytes], b: list[bytes]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--raw-csv", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--branch-threshold", type=float, default=0.95)
    p.add_argument("--coverage-threshold", type=float, default=0.05)
    p.add_argument("--block-size", type=int, default=128)
    p.add_argument("--max-decoded-blocks", type=int, default=None,
                   help="Cap decoded blocks per chain (default: full chain)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    csv_files = discover_csv_files(args.raw_csv)
    if not csv_files:
        print(f"No CSV files found at {args.raw_csv}", file=sys.stderr)
        sys.exit(1)
    print(f"Input: {len(csv_files)} CSV file(s) under {args.raw_csv}", flush=True)

    # ---- Phase 1: build global trie + per-user tries in one pass ----
    t0 = time.time()
    global_root = TrieNode()
    user_roots: dict[str, TrieNode] = defaultdict(TrieNode)
    user_request_count: dict[str, int] = defaultdict(int)
    n_total = 0
    n_blocks_total = 0
    n_empty = 0

    for request_id, user_id, raw_prompt, _ts in iter_raw_records(csv_files):
        n_total += 1
        user_request_count[user_id] += 1

        if not raw_prompt:
            n_empty += 1
            global_root.count += 1
            user_roots[user_id].count += 1
            continue

        blocks = split_blocks(raw_prompt, args.block_size)
        keys = compute_prefix_path_keys(blocks)
        n_blocks_total += len(keys)

        trie_insert(global_root, keys, request_id)
        trie_insert(user_roots[user_id], keys, request_id)

        if n_total % 1000 == 0:
            print(f"  {n_total:>7,} requests  ({len(user_roots)} users)", flush=True)

    build_s = time.time() - t0
    print(
        f"Built global + {len(user_roots)} per-user tries "
        f"({n_total:,} requests, {n_blocks_total:,} blocks) in {build_s:.1f}s "
        f"(empty prompts: {n_empty})",
        flush=True,
    )

    # ---- Phase 2: find LCP for global + each user ----
    t1 = time.time()
    global_chain, global_branch_points = find_lcp(
        global_root, args.branch_threshold, args.coverage_threshold,
    )
    global_keys = [k for k, _ in global_chain]

    # Build per-user chain results (without decoded content yet)
    user_results = []
    decode_request_ids: dict[str, int] = {}  # rid -> max blocks to decode

    for user_id, root in user_roots.items():
        chain, branch_points = find_lcp(
            root, args.branch_threshold, args.coverage_threshold,
        )
        chain_keys = [k for k, _ in chain]
        prefix_match = common_prefix_length(chain_keys, global_keys)
        same_as_global = (chain_keys == global_keys)

        sample_id = None
        if chain:
            sample_id = find_sample_request_at_depth(root, chain)
            if sample_id:
                n_decode = (
                    len(chain) if args.max_decoded_blocks is None
                    else min(len(chain), args.max_decoded_blocks)
                )
                # Track max blocks needed per request_id
                decode_request_ids[sample_id] = max(
                    decode_request_ids.get(sample_id, 0), n_decode
                )

        user_results.append({
            "_user_id": user_id,
            "_chain_keys": chain_keys,
            "_chain_counts": [c for _, c in chain],
            "_branch_points": branch_points,
            "_sample_request_id": sample_id,
            "_prefix_match_with_global": prefix_match,
            "_same_as_global": same_as_global,
        })

    # Add global decode request
    global_sample_id = None
    if global_chain:
        global_sample_id = find_sample_request_at_depth(global_root, global_chain)
        if global_sample_id:
            n_decode = (
                len(global_chain) if args.max_decoded_blocks is None
                else min(len(global_chain), args.max_decoded_blocks)
            )
            decode_request_ids[global_sample_id] = max(
                decode_request_ids.get(global_sample_id, 0), n_decode
            )

    print(
        f"Found {len(global_chain)}-block global chain + "
        f"{len(user_results)} per-user chains  ({time.time()-t1:.2f}s)",
        flush=True,
    )

    # ---- Phase 3: batch decode all needed chain contents in one CSV pass ----
    t2 = time.time()
    decoded_map = batch_decode(csv_files, decode_request_ids, args.block_size)
    print(
        f"Decoded {len(decoded_map)}/{len(decode_request_ids)} chain samples "
        f"({time.time()-t2:.2f}s)",
        flush=True,
    )

    # ---- Phase 4: assemble output ----
    def build_lcp_content(keys, counts, denom: int, decoded: list[str]) -> list[dict]:
        return [
            {
                "position": i,
                "prefix_path_key": k.hex(),
                "count": c,
                "coverage_pct": round(c / denom * 100, 3) if denom else 0.0,
                "decoded_text": decoded[i] if i < len(decoded) else None,
            }
            for i, (k, c) in enumerate(zip(keys, counts))
        ]

    # Sort users by request count desc
    user_results.sort(key=lambda u: -user_request_count[u["_user_id"]])

    users_out = []
    for u in user_results:
        uid = u["_user_id"]
        rcount = user_request_count[uid]
        chain_keys = u["_chain_keys"]
        chain_counts = u["_chain_counts"]
        sample_id = u["_sample_request_id"]
        decoded = decoded_map.get(sample_id, []) if sample_id else []

        users_out.append({
            "user_id": uid,
            "request_count": rcount,
            "chain_length": len(chain_keys),
            "chain_coverage_count": chain_counts[-1] if chain_counts else 0,
            "chain_coverage_pct": (
                chain_counts[-1] / rcount * 100 if (chain_counts and rcount) else 0.0
            ),
            "prefix_match_with_global": u["_prefix_match_with_global"],
            "same_as_global": u["_same_as_global"],
            "sample_request_id": sample_id,
            "lcp_content": build_lcp_content(chain_keys, chain_counts, rcount, decoded),
            "branch_points": u["_branch_points"],
        })

    # User aggregate stats
    n_users = len(users_out)
    n_with_chain = sum(1 for u in users_out if u["chain_length"] > 0)
    n_match_full = sum(1 for u in users_out if u["same_as_global"])
    n_match_50pct = sum(
        1 for u in users_out
        if len(global_keys) > 0
        and u["prefix_match_with_global"] >= 0.5 * len(global_keys)
    )
    n_unique_chain = sum(
        1 for u in users_out
        if u["chain_length"] > 0 and u["prefix_match_with_global"] == 0
    )

    # Global chain content
    global_decoded = decoded_map.get(global_sample_id, []) if global_sample_id else []
    global_counts = [c for _, c in global_chain]

    output = {
        "input": str(args.raw_csv),
        "input_files": [str(p) for p in csv_files],
        "params": {
            "branch_threshold": args.branch_threshold,
            "coverage_threshold": args.coverage_threshold,
            "block_size": args.block_size,
        },
        "stats": {
            "total_requests": n_total,
            "total_users": n_users,
            "total_blocks": n_blocks_total,
            "empty_prompts": n_empty,
            "build_seconds": round(build_s, 2),
        },
        "user_aggregate": {
            "users_with_chain": n_with_chain,
            "users_with_no_chain": n_users - n_with_chain,
            "users_matching_global_fully": n_match_full,
            "users_matching_global_50pct_prefix": n_match_50pct,
            "users_with_unique_chain": n_unique_chain,
        },
        "global_chain": {
            "chain_length": len(global_chain),
            "chain_coverage_count": global_counts[-1] if global_counts else 0,
            "chain_coverage_pct": (
                global_counts[-1] / n_total * 100 if (global_counts and n_total) else 0.0
            ),
            "sample_request_id": global_sample_id,
            "lcp_content": build_lcp_content(
                global_keys, global_counts, n_total, global_decoded,
            ),
            "branch_points": global_branch_points,
        },
        "users": users_out,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # ---- Console summary ----
    print("\n=== Result ===", flush=True)
    print(f"  total requests       : {n_total:,}", flush=True)
    print(f"  total users          : {n_users}", flush=True)
    print(f"  global chain length  : {len(global_chain)}", flush=True)
    if global_chain:
        cov = global_counts[-1] / n_total * 100
        print(f"  global chain coverage: {global_counts[-1]:,}/{n_total:,}  ({cov:.1f}%)",
              flush=True)
    print(f"\n  Users with chain     : {n_with_chain}/{n_users}", flush=True)
    print(f"  Users matching global fully     : {n_match_full}", flush=True)
    print(f"  Users matching ≥50%-prefix      : {n_match_50pct}", flush=True)
    print(f"  Users with unique (non-global)  : {n_unique_chain}", flush=True)

    # Per-user table (top 30)
    print(f"\n  Per-user chain summary (sorted by request count):", flush=True)
    print(
        f"    {'user_id':<32} {'reqs':>8} {'chain':>6} {'cov%':>6} {'vs_global':>12}",
        flush=True,
    )
    print(f"    {'-'*32} {'-'*8} {'-'*6} {'-'*6} {'-'*12}", flush=True)
    for u in users_out[:30]:
        if u["same_as_global"]:
            tag = "= global"
        elif u["chain_length"] == 0:
            tag = "(no chain)"
        elif u["prefix_match_with_global"] == 0:
            tag = "unique"
        else:
            tag = f"prefix:{u['prefix_match_with_global']}"
        uid_disp = u["user_id"] if len(u["user_id"]) <= 32 else u["user_id"][:29] + "..."
        print(
            f"    {uid_disp:<32} {u['request_count']:>8,} "
            f"{u['chain_length']:>6} {u['chain_coverage_pct']:>5.1f}% {tag:>12}",
            flush=True,
        )
    if len(users_out) > 30:
        print(f"    ... ({len(users_out)-30} more)", flush=True)

    print(f"\n  output → {args.output}", flush=True)


if __name__ == "__main__":
    main()
