#!/usr/bin/env python3
"""Step 1.5 — Multi-chain forest finder.

Pure algorithm primitive: given a trie root, recursively explore all
sub-paths satisfying the multi-chain thresholds and return a forest of
"strong" chains rather than a single greedy path.

Designed as a reusable building block:
  - Called in-process by per_user_report_analyzer.py (one trie per user)
  - Can be invoked directly on raw CSV for global-scope multi-chain
    exploration / debugging

Reusable across models; tokenizer-free; offline-safe.

Algorithm spec: docs/per_user_research_design.md §5
Threshold rationale: §5.3 (default 0.05 / 0.05, fully decoupled from
single-chain's 0.45 to avoid silently dropping minority system prompts
which cluster at 5-15% root-ratio for GLM / DS-8K / DS-32K).

Standalone CLI usage
--------------------
  python scripts/multi_chain_finder.py \\
      --raw-csv data/<dataset>/raw \\
      --output  outputs/<dataset>/chain_forest_global.json \\
      --mc-branch-threshold     0.05 \\
      --mc-coverage-threshold   0.05 \\
      --mc-min-chain-length     10 \\
      --mc-min-chain-coverage   0.01 \\
      --mc-max-chains           50
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Reuse Step 1.1 primitives + the centralized chain algorithm (lib.chains)
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.chains import (  # noqa: E402
    DEFAULT_MAX_CHAINS,
    DEFAULT_MC_BRANCH_THRESHOLD,
    DEFAULT_MC_COVERAGE_THRESHOLD,
    DEFAULT_MIN_CHAIN_COVERAGE,
    DEFAULT_MIN_CHAIN_LENGTH,
    TrieNode,
    find_chain_forest,
    trie_insert,
)
from verify_chain_path_closure import (  # noqa: E402
    compute_prefix_path_keys,
    discover_csv_files,
    iter_raw_records,
    split_blocks,
)


# ---------------------------------------------------------------------------
# Standalone CLI (debug + global-trie exploration)
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--raw-csv", required=True, type=Path,
                   help="Raw CSV file or directory")
    p.add_argument("--output", required=True, type=Path,
                   help="Path for chain_forest JSON")
    p.add_argument("--mc-branch-threshold", type=float,
                   default=DEFAULT_MC_BRANCH_THRESHOLD)
    p.add_argument("--mc-coverage-threshold", type=float,
                   default=DEFAULT_MC_COVERAGE_THRESHOLD)
    p.add_argument("--mc-min-chain-length", type=int,
                   default=DEFAULT_MIN_CHAIN_LENGTH)
    p.add_argument("--mc-min-chain-coverage", type=float,
                   default=DEFAULT_MIN_CHAIN_COVERAGE)
    p.add_argument("--mc-max-chains", type=int,
                   default=DEFAULT_MAX_CHAINS)
    p.add_argument("--block-size", type=int, default=128)
    return p.parse_args()


def main() -> None:
    args = parse_args()

    csv_files = discover_csv_files(args.raw_csv)
    if not csv_files:
        print(f"error: no CSV files at {args.raw_csv}", file=sys.stderr)
        sys.exit(1)
    print(f"Input: {len(csv_files)} CSV file(s) under {args.raw_csv}",
          flush=True)

    # Build global trie
    t0 = time.time()
    root = TrieNode()
    n_total = 0
    n_empty = 0
    n_blocks = 0
    for request_id, _user_id, raw_prompt, _ts in iter_raw_records(csv_files):
        n_total += 1
        if not raw_prompt:
            n_empty += 1
            root.count += 1
            continue
        blocks = split_blocks(raw_prompt, args.block_size)
        keys = compute_prefix_path_keys(blocks)
        n_blocks += len(keys)
        trie_insert(root, keys, request_id)
        if n_total % 1000 == 0:
            print(f"  {n_total:>7,} requests", flush=True)
    build_s = time.time() - t0
    print(
        f"Built trie: {n_total:,} requests, {n_blocks:,} blocks, "
        f"empty={n_empty} ({build_s:.1f}s)",
        flush=True,
    )

    # Find forest
    t1 = time.time()
    forest = find_chain_forest(
        root,
        mc_branch_thr=args.mc_branch_threshold,
        mc_cov_thr=args.mc_coverage_threshold,
        min_chain_length=args.mc_min_chain_length,
        min_chain_coverage=args.mc_min_chain_coverage,
        max_chains=args.mc_max_chains,
    )
    forest["params"]["block_size"] = args.block_size
    forest["stats"]["build_seconds"] = round(build_s, 2)
    forest["stats"]["forest_seconds"] = round(time.time() - t1, 2)
    forest["input"] = {
        "raw_csv": str(args.raw_csv),
        "files":   [str(p) for p in csv_files],
        "empty_prompts": n_empty,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(forest, f, indent=2, ensure_ascii=False)

    # Console summary
    s = forest["stats"]
    print("\n=== Chain forest ===", flush=True)
    print(f"  raw chains       : {s['total_chains_before_pruning']:>6}", flush=True)
    print(f"  after length     : {s['total_chains_after_length_pruning']:>6}",
          flush=True)
    print(f"  after coverage   : {s['total_chains_after_coverage_pruning']:>6}",
          flush=True)
    print(f"  after max_chains : {s['total_chains_after_max_cap']:>6}",
          flush=True)

    if forest["chains"]:
        print("\n  Top chains (sorted by coverage):", flush=True)
        print(
            f"    {'id':>3} {'length':>6} {'count':>8} {'cov%':>6} "
            f"{'branch_pos':>10} {'branch_r':>10}",
            flush=True,
        )
        for c in forest["chains"][:20]:
            bp = c["branch_at_root_position"]
            br = c["branch_at_root_ratio"]
            print(
                f"    {c['chain_id']:>3} {c['chain_length']:>6} "
                f"{c['coverage_count']:>8} {c['coverage_pct']:>5.1f}% "
                f"{(str(bp) if bp is not None else '-'):>10} "
                f"{(f'{br:.3f}' if br is not None else '-'):>10}",
                flush=True,
            )
        if len(forest["chains"]) > 20:
            print(f"    ... {len(forest['chains'])-20} more", flush=True)

    print(f"\n  output → {args.output}", flush=True)


if __name__ == "__main__":
    main()
