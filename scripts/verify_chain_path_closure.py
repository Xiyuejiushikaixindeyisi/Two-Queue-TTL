#!/usr/bin/env python3
"""Step 1.1 — Strict path-closed chain detection from raw CSV.

Builds a prefix-path trie directly from raw_prompt and finds the longest
path-closed chain using two independent thresholds:

  branch_threshold   : max_child.count / parent.count >= X  → continue chain
  coverage_threshold : node.count / total_requests >= X     → still significant

Output: JSON with chain length, coverage, decoded text content per block,
and a list of branch points (positions where the chain was forced to stop).

Reusable across models. Consumes raw CSV in the standard 4-column format:
  request_id, user_id, raw_prompt, timestamp

Does NOT depend on convert_raw_trace.py — chunking + hashing happen in-memory.
Tokenizer-free: blocks are 128-byte utf8 slices; decoding uses
errors='replace' to handle codepoint splits.

Usage
-----
  python scripts/verify_chain_path_closure.py \
      --raw-csv data/dsk8k_2h_5k/raw \
      --output  outputs/dsk8k_2h_5k/chain_summary.json
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

# Some raw_prompt fields can be very large; lift csv field size cap.
csv.field_size_limit(sys.maxsize)


# ---------------------------------------------------------------------------
# Hashing & chunking
# ---------------------------------------------------------------------------

def split_blocks(raw_prompt: str, block_size: int) -> list[bytes]:
    """Slice raw_prompt utf8 bytes into fixed-size blocks (no padding)."""
    encoded = raw_prompt.encode("utf-8")
    return [encoded[i:i + block_size] for i in range(0, len(encoded), block_size)]


def compute_prefix_path_keys(blocks: list[bytes]) -> list[bytes]:
    """K_0 = SHA256(B_0); K_n = SHA256(K_{n-1} || B_n)."""
    keys: list[bytes] = []
    prev = b""
    for block in blocks:
        h = hashlib.sha256()
        h.update(prev)
        h.update(block)
        prev = h.digest()
        keys.append(prev)
    return keys


# ---------------------------------------------------------------------------
# Trie
# ---------------------------------------------------------------------------

@dataclass
class TrieNode:
    count: int = 0
    children: dict = field(default_factory=dict)
    sample_request_id: Optional[str] = None  # any request following this path


def trie_insert(root: TrieNode, keys: list[bytes], request_id: str) -> None:
    node = root
    node.count += 1
    if node.sample_request_id is None:
        node.sample_request_id = request_id
    for key in keys:
        child = node.children.get(key)
        if child is None:
            child = TrieNode()
            node.children[key] = child
        child.count += 1
        if child.sample_request_id is None:
            child.sample_request_id = request_id
        node = child


# ---------------------------------------------------------------------------
# LCP discovery
# ---------------------------------------------------------------------------

def find_lcp(
    root: TrieNode,
    branch_threshold: float,
    coverage_threshold: float,
) -> tuple[list[tuple[bytes, int]], list[dict]]:
    """Greedy walk from root along the heaviest child.

    Stops when EITHER threshold fails. Records the failure as a branch point.
    Returns (chain, branch_points).
    """
    total = root.count
    chain: list[tuple[bytes, int]] = []
    branch_points: list[dict] = []
    node = root
    pos = 0

    while node.children:
        # Pick the heaviest child
        max_key, max_child = max(node.children.items(), key=lambda kv: kv[1].count)

        cov = max_child.count / total
        ratio = max_child.count / node.count if node.count else 0.0

        if cov < coverage_threshold:
            branch_points.append({
                "position": pos,
                "reason": "coverage_threshold",
                "max_child_coverage": cov,
                "parent_count": node.count,
                "max_child_count": max_child.count,
            })
            break

        if ratio < branch_threshold:
            sorted_children = sorted(
                node.children.values(), key=lambda c: c.count, reverse=True
            )
            branch_points.append({
                "position": pos,
                "reason": "branch_threshold",
                "parent_count": node.count,
                "max_child_count": max_child.count,
                "max_child_ratio": ratio,
                "num_significant_children": sum(
                    1 for c in node.children.values() if c.count / total >= coverage_threshold
                ),
                "top_3_children_counts": [c.count for c in sorted_children[:3]],
            })
            break

        chain.append((max_key, max_child.count))
        node = max_child
        pos += 1

    return chain, branch_points


def find_sample_request_at_depth(
    root: TrieNode, chain: list[tuple[bytes, int]]
) -> Optional[str]:
    """Walk the trie along the chain to get the deepest sample_request_id."""
    node = root
    for key, _ in chain:
        node = node.children[key]
    return node.sample_request_id


# ---------------------------------------------------------------------------
# Decoding (re-read raw to extract chain content)
# ---------------------------------------------------------------------------

def decode_chain_content(
    csv_files: list[Path],
    target_request_id: str,
    chain_length: int,
    block_size: int,
) -> list[str]:
    """Locate target_request_id and decode its first chain_length blocks."""
    for csv_path in csv_files:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("request_id") == target_request_id:
                    blocks = split_blocks(row["raw_prompt"], block_size)
                    return [
                        b.decode("utf-8", errors="replace")
                        for b in blocks[:chain_length]
                    ]
    return []


# ---------------------------------------------------------------------------
# Raw CSV iteration
# ---------------------------------------------------------------------------

def discover_csv_files(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(path.glob("*.csv"))
    return [path]


def iter_raw_records(csv_files: list[Path]) -> Iterator[tuple[str, str, str, str]]:
    expected = {"request_id", "user_id", "raw_prompt", "timestamp"}
    for csv_path in csv_files:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            missing = expected - set(reader.fieldnames or [])
            if missing:
                raise ValueError(
                    f"{csv_path}: missing required columns {missing}. "
                    f"Found: {reader.fieldnames}"
                )
            for row in reader:
                yield (
                    row["request_id"],
                    row["user_id"],
                    row["raw_prompt"],
                    row["timestamp"],
                )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--raw-csv", required=True, type=Path,
                   help="Raw CSV file or directory of CSV files (4-column format)")
    p.add_argument("--output", required=True, type=Path,
                   help="Output JSON path")
    p.add_argument("--branch-threshold", type=float, default=0.95,
                   help="max_child.count / parent.count threshold (default 0.95)")
    p.add_argument("--coverage-threshold", type=float, default=0.05,
                   help="node.count / total_requests threshold (default 0.05)")
    p.add_argument("--block-size", type=int, default=128,
                   help="Bytes per block (default 128)")
    p.add_argument("--max-decoded-blocks", type=int, default=None,
                   help="Cap how many chain blocks to decode (default: full chain)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    csv_files = discover_csv_files(args.raw_csv)
    if not csv_files:
        print(f"No CSV files found at {args.raw_csv}", file=sys.stderr)
        sys.exit(1)
    print(f"Input: {len(csv_files)} CSV file(s) under {args.raw_csv}", flush=True)

    # Phase 1 — read + build trie
    t0 = time.time()
    root = TrieNode()
    n_requests = 0
    n_blocks = 0
    n_empty = 0

    for request_id, _user_id, raw_prompt, _ts in iter_raw_records(csv_files):
        if not raw_prompt:
            n_empty += 1
            n_requests += 1
            root.count += 1  # still count the request at root
            continue
        blocks = split_blocks(raw_prompt, args.block_size)
        keys = compute_prefix_path_keys(blocks)
        trie_insert(root, keys, request_id)
        n_requests += 1
        n_blocks += len(keys)
        if n_requests % 1000 == 0:
            print(f"  {n_requests:>7,} requests  ({n_blocks:>10,} blocks)", flush=True)

    build_s = time.time() - t0
    print(
        f"Trie built: {n_requests:,} requests, {n_blocks:,} blocks "
        f"in {build_s:.1f}s  (empty prompts: {n_empty})",
        flush=True,
    )

    # Phase 2 — find LCP
    t1 = time.time()
    chain, branch_points = find_lcp(
        root,
        args.branch_threshold,
        args.coverage_threshold,
    )
    print(
        f"LCP found: length={len(chain)}, branch_points={len(branch_points)}  "
        f"({time.time()-t1:.2f}s)",
        flush=True,
    )

    # Phase 3 — decode
    decoded: list[str] = []
    sample_id: Optional[str] = None
    if chain:
        sample_id = find_sample_request_at_depth(root, chain)
        if sample_id is not None:
            t2 = time.time()
            n_decode = (
                len(chain) if args.max_decoded_blocks is None
                else min(len(chain), args.max_decoded_blocks)
            )
            decoded = decode_chain_content(csv_files, sample_id, n_decode, args.block_size)
            print(
                f"Decoded {len(decoded)}/{len(chain)} chain blocks "
                f"from request_id={sample_id}  ({time.time()-t2:.2f}s)",
                flush=True,
            )

    # Phase 4 — output
    output = {
        "input": str(args.raw_csv),
        "input_files": [str(p) for p in csv_files],
        "params": {
            "branch_threshold": args.branch_threshold,
            "coverage_threshold": args.coverage_threshold,
            "block_size": args.block_size,
        },
        "stats": {
            "total_requests": n_requests,
            "total_blocks": n_blocks,
            "empty_prompts": n_empty,
            "build_seconds": round(build_s, 2),
        },
        "lcp": {
            "chain_length": len(chain),
            "chain_coverage_count": chain[-1][1] if chain else 0,
            "chain_coverage_pct": (chain[-1][1] / n_requests * 100) if chain else 0.0,
            "sample_request_id": sample_id,
        },
        "lcp_content": [
            {
                "position": i,
                "prefix_path_key": k.hex(),
                "count": c,
                "coverage_pct": round(c / n_requests * 100, 3),
                "decoded_text": decoded[i] if i < len(decoded) else None,
            }
            for i, (k, c) in enumerate(chain)
        ],
        "branch_points": branch_points,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # Console summary
    print("\n=== Result ===", flush=True)
    print(f"  total requests       : {n_requests:,}", flush=True)
    print(f"  chain length         : {len(chain)}", flush=True)
    if chain:
        cov_pct = chain[-1][1] / n_requests * 100
        print(
            f"  chain coverage       : {chain[-1][1]:,}/{n_requests:,}  ({cov_pct:.1f}%)",
            flush=True,
        )
    print(f"  branch points        : {len(branch_points)}", flush=True)
    if branch_points:
        bp = branch_points[0]
        print(f"    first reason       : {bp['reason']} at pos={bp['position']}", flush=True)
    print(f"  output → {args.output}", flush=True)


if __name__ == "__main__":
    main()
