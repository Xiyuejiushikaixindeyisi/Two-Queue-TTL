#!/usr/bin/env python3
"""Generate an offline block registry from a historical trace.

What this script does
---------------------
1. Replay the trace, compute prefix-path keys for every request
   (same SHA-256 chain used by PrefixCache and TwoQueueTTLPolicy).
2. Count how many *distinct requests* each prefix-path key appears in.
3. Identify the longest "common prefix chain" — the consecutive run of
   blocks starting at position 0 that appears in ≥ min_request_fraction
   of all requests.  This chain corresponds to the shared system prompt /
   Agent instruction prefix.
4. Optionally also write the top-N individually hot blocks (not
   necessarily forming a consecutive chain from position 0).

Output format (TSV)
-------------------
    # prefix_path_key  request_count  block_pos  description
    a3f2c8deadbeef00   8412           0           chain_pos=0,top-1
    b7d1e9cafebabe01   8401           1           chain_pos=1,top-1
    ...

The output file is consumed by OfflineRegistry.from_files() which does
O(1) set-membership lookup by prefix_path_key (hex string).

Key-type correctness
--------------------
TwoQueueTTLPolicy.add() calls:
    self._registry.is_protected(block_key)      ← block_key is prefix-path hex

So registry entries MUST be prefix-path hex strings, NOT raw hash_ids[i].
This script uses make_prefix_path_keys() to produce the correct keys.

Example
-------
    # Step 1: convert raw trace (if needed)
    python scripts/convert_raw_trace.py \\
        --input data/raw.csv --output data/trace.csv

    # Step 2: generate registry from converted trace
    python scripts/generate_registry.py \\
        --trace data/trace.csv \\
        --model-id default \\
        --top-n 500 \\
        --min-fraction 0.10 \\
        --output data/protected_registry.txt

    # Step 3: use in simulation
    python scripts/run_simulation.py \\
        --trace data/trace.csv \\
        --policy two_queue_ttl \\
        --registry data/protected_registry.txt \\
        --capacity 10000
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.core.prefix_key import make_prefix_path_keys
from sim.io.trace_loader import load_trace


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate offline registry of hot prefix-path blocks",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--trace", required=True, help="Converted trace CSV (with hash_ids)")
    p.add_argument("--model-id", default="default",
                   help="model_id used during prefix-key computation (must match simulation)")
    p.add_argument("--top-n", type=int, default=200, dest="top_n",
                   help="Number of individually hot blocks to include in the registry")
    p.add_argument("--min-fraction", type=float, default=0.05, dest="min_fraction",
                   help="Common-prefix chain: include a chain block if it appears in "
                        "at least this fraction of all requests (0.05 = 5%%)")
    p.add_argument("--output", required=True, help="Output registry file path (.txt)")
    p.add_argument("--raw", action="store_true",
                   help="Input CSV has raw_prompt column; tokenize automatically")
    p.add_argument("--block-size", type=int, default=128,
                   help="Tokens per block (used only when --raw is set)")
    return p.parse_args()


def build_frequency_index(trace, model_id: str):
    """
    Returns
    -------
    key_request_count : Counter[hex_str → number of distinct requests containing this key]
    key_pos           : dict[hex_str → block_pos in its chain]
    total_requests    : int
    """
    key_request_count: Counter = Counter()
    key_pos: dict[str, int] = {}

    for record in trace:
        # Override model_id if the trace was generated with a fixed constant
        effective_model_id = model_id if model_id != "from_trace" else record.model_id
        keys = make_prefix_path_keys(effective_model_id, record.hash_ids)
        for pos, key in enumerate(keys):
            hex_key = key.hex()
            key_request_count[hex_key] += 1
            if hex_key not in key_pos:
                key_pos[hex_key] = pos

    return key_request_count, key_pos, len(trace)


def find_common_prefix_chain(
    key_request_count: Counter,
    key_pos: dict,
    total_requests: int,
    min_fraction: float,
    trace,
    model_id: str,
) -> list[tuple[str, int, int]]:
    """
    Find the longest consecutive chain starting at pos=0 where each block
    appears in >= min_fraction of all requests.

    A 'chain' here means: for a given request's prefix-path keys, the block at
    position i is part of the chain if it appears in >= min_fraction of all
    requests AND the block at position i-1 also qualifies.

    Returns list of (hex_key, request_count, block_pos) sorted by pos.
    """
    min_count = int(total_requests * min_fraction)

    # Collect (pos → hex_key → count) from the most common key at each pos
    pos_candidates: dict[int, Counter] = defaultdict(Counter)
    for record in trace:
        effective_model_id = model_id if model_id != "from_trace" else record.model_id
        keys = make_prefix_path_keys(effective_model_id, record.hash_ids)
        for pos, key in enumerate(keys):
            pos_candidates[pos][key.hex()] += 1

    chain = []
    for pos in sorted(pos_candidates.keys()):
        # Take the single most frequent key at this position
        most_common_key, count = pos_candidates[pos].most_common(1)[0]
        if count < min_count:
            break   # chain broken: this position doesn't meet the threshold
        chain.append((most_common_key, count, pos))

    return chain


def main() -> None:
    args = parse_args()

    if args.raw:
        from sim.io.raw_trace_loader import load_raw_trace
        trace = load_raw_trace(args.trace, block_size=args.block_size)
    else:
        trace = load_trace(args.trace)

    print(f"Loaded {len(trace):,} records")
    print(f"Computing prefix-path key frequencies (model_id='{args.model_id}') …")

    key_request_count, key_pos, total = build_frequency_index(trace, args.model_id)
    print(f"  unique prefix-path keys seen: {len(key_request_count):,}")

    # ── Common prefix chain ───────────────────────────────────────────────
    chain = find_common_prefix_chain(
        key_request_count, key_pos, total, args.min_fraction, trace, args.model_id
    )
    print(f"\nCommon prefix chain (≥{args.min_fraction:.0%} of {total:,} requests):")
    if chain:
        for hex_key, count, pos in chain:
            pct = count / total * 100
            print(f"  pos={pos:3d}  {hex_key}  count={count:,}  ({pct:.1f}%)")
    else:
        print("  (none found — all positions below min_fraction threshold)")

    # ── Top-N individually hot blocks ─────────────────────────────────────
    top_n = key_request_count.most_common(args.top_n)
    print(f"\nTop-{args.top_n} hot blocks by request count:")
    for i, (hex_key, count) in enumerate(top_n[:10], 1):
        pct = count / total * 100
        pos = key_pos.get(hex_key, -1)
        print(f"  #{i:3d}  pos={pos:3d}  count={count:,} ({pct:.1f}%)  {hex_key}")
    if len(top_n) > 10:
        print(f"  … ({len(top_n) - 10} more, written to file)")

    # ── Write registry file ───────────────────────────────────────────────
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    # Union: chain blocks + top-N blocks (deduplicated)
    registry_entries: dict[str, tuple[int, int, str]] = {}  # hex → (count, pos, label)

    for hex_key, count, pos in chain:
        registry_entries[hex_key] = (count, pos, f"chain_pos={pos}")

    for i, (hex_key, count) in enumerate(top_n, 1):
        if hex_key not in registry_entries:
            pos = key_pos.get(hex_key, -1)
            registry_entries[hex_key] = (count, pos, f"top-{i}")

    # Sort by (block_pos asc, count desc) for readability
    sorted_entries = sorted(
        registry_entries.items(),
        key=lambda kv: (kv[1][1], -kv[1][0])
    )

    with open(args.output, "w") as f:
        f.write(f"# Offline registry generated from: {args.trace}\n")
        f.write(f"# total_requests={total}  min_fraction={args.min_fraction}"
                f"  top_n={args.top_n}\n")
        f.write(f"# model_id={args.model_id}\n")
        f.write("# prefix_path_key\trequest_count\tblock_pos\tdescription\n")
        for hex_key, (count, pos, label) in sorted_entries:
            f.write(f"{hex_key}\t{count}\t{pos}\t{label}\n")

    print(f"\nRegistry written: {args.output}  ({len(sorted_entries)} entries)")
    print(f"  chain blocks  : {len(chain)}")
    print(f"  top-N blocks  : {len(registry_entries) - len(chain)}")


if __name__ == "__main__":
    main()
