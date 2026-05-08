#!/usr/bin/env python3
"""Step 1.3 — Cross-window chain stability analyzer.

Quantifies how prefix-path chains drift across time windows. Builds the
trie + LCP for each sample directly from raw CSV (reusing the Step 1.1
primitives), so `--branch-threshold` and other chain parameters can be
set on the command line without re-running Step 1.2 first.

Reusable across models — input is a list of LABEL=DIR pairs naming raw
CSV directories. Tokenizer-free; offline-safe.

Usage
-----
  python scripts/chain_stability_analyzer.py \\
      --raw-csv 0506=data/dsk8k_24h_0506/raw \\
                0507=data/dsk8k_24h_0507/raw \\
      --branch-threshold 0.40 \\
      --top-n 20 \\
      --output outputs/dsk8k_step1_3/chain_stability_report.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Iterable

# Reuse Step 1.1 primitives directly (same trie / hashing / LCP finder).
sys.path.insert(0, str(Path(__file__).parent))
from verify_chain_path_closure import (  # noqa: E402
    TrieNode,
    compute_prefix_path_keys,
    discover_csv_files,
    find_lcp,
    iter_raw_records,
    split_blocks,
    trie_insert,
)


# ---------------------------------------------------------------------------
# Sample (one raw-CSV dataset → trie → chain keys)
# ---------------------------------------------------------------------------

class Sample:
    """One raw CSV directory, indexed for stability comparison.

    Calling .build() runs the same Step 1.1 + 1.2 trie/LCP pipeline that
    `verify_chain_path_closure.py` and `per_user_chain_analyzer.py` use,
    so chain definitions stay consistent across the three steps.
    """

    def __init__(self, label: str, csv_dir: Path):
        self.label = label
        self.csv_dir = csv_dir
        self.global_chain_keys: list[str] = []
        self.user_chains: dict[str, list[str]] = {}
        self.total_requests: int = 0
        self.total_users: int = 0
        self.empty_prompts: int = 0
        self.build_seconds: float = 0.0

    def build(self, branch_threshold: float, coverage_threshold: float,
              block_size: int) -> None:
        csv_files = discover_csv_files(self.csv_dir)
        if not csv_files:
            raise FileNotFoundError(
                f"sample {self.label!r}: no CSV files under {self.csv_dir}"
            )

        t0 = time.time()
        global_root = TrieNode()
        user_roots: dict[str, TrieNode] = defaultdict(TrieNode)
        n_total = 0
        n_empty = 0

        for request_id, user_id, raw_prompt, _ts in iter_raw_records(csv_files):
            n_total += 1
            if not raw_prompt:
                n_empty += 1
                global_root.count += 1
                user_roots[user_id].count += 1
                continue
            blocks = split_blocks(raw_prompt, block_size)
            keys = compute_prefix_path_keys(blocks)
            trie_insert(global_root, keys, request_id)
            trie_insert(user_roots[user_id], keys, request_id)

        global_chain, _ = find_lcp(
            global_root, branch_threshold, coverage_threshold,
        )
        self.global_chain_keys = [k.hex() for k, _ in global_chain]

        self.user_chains = {}
        for uid, root in user_roots.items():
            chain, _ = find_lcp(root, branch_threshold, coverage_threshold)
            self.user_chains[uid] = [k.hex() for k, _ in chain]

        self.total_requests = n_total
        self.total_users = len(user_roots)
        self.empty_prompts = n_empty
        self.build_seconds = time.time() - t0


def parse_sample_specs(spec_pairs: list[str]) -> list[Sample]:
    """Parse `LABEL=DIR` raw-csv specs."""
    samples: list[Sample] = []
    seen_labels: set[str] = set()
    for spec in spec_pairs:
        if "=" not in spec:
            raise ValueError(
                f"--raw-csv value {spec!r} is not LABEL=DIR (e.g. 0506=data/.../raw)"
            )
        label, _, raw_path = spec.partition("=")
        label = label.strip()
        path = Path(raw_path.strip())
        if not label:
            raise ValueError(f"empty label in --raw-csv value {spec!r}")
        if label in seen_labels:
            raise ValueError(f"duplicate sample label: {label!r}")
        seen_labels.add(label)
        if not path.exists():
            raise FileNotFoundError(f"sample {label!r}: {path} does not exist")
        samples.append(Sample(label, path))
    if len(samples) < 2:
        raise ValueError(
            f"chain stability analysis needs ≥2 samples; got {len(samples)}"
        )
    return samples


# ---------------------------------------------------------------------------
# Core similarity primitives
# ---------------------------------------------------------------------------

def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Standard set-Jaccard. Empty ∩ empty = 1.0 by convention."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    union = sa | sb
    if not union:
        return 1.0
    return len(sa & sb) / len(union)


def common_prefix_length(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def stability_tier(mean_jaccard: float) -> str:
    """Maps mean Jaccard to plan §2.2/1.3 tiers.

    The plan thresholds were specified for 7-day stability; the same cutoffs
    are applied to whatever windows the caller supplied. Tier interpretation
    should consider the actual time spans of the inputs.
    """
    if mean_jaccard >= 0.95:
        return "high"           # 静态 pin 可行
    if mean_jaccard >= 0.70:
        return "slow_drift"     # 静态 pin + 周级更新
    if mean_jaccard >= 0.40:
        return "medium_drift"   # 必须动态准入/退出
    return "unstable"           # chain 优化方向不可行


# ---------------------------------------------------------------------------
# Pairwise comparisons
# ---------------------------------------------------------------------------

def compare_global_chain(a: Sample, b: Sample, top_n: int) -> dict:
    a_keys = a.global_chain_keys[:top_n]
    b_keys = b.global_chain_keys[:top_n]
    return {
        "sample_a": a.label,
        "sample_b": b.label,
        "top_n": top_n,
        "len_a": len(a_keys),
        "len_b": len(b_keys),
        "common_prefix_length": common_prefix_length(a_keys, b_keys),
        "jaccard": round(jaccard(a_keys, b_keys), 4),
    }


def compare_per_user(a: Sample, b: Sample, top_n: int) -> dict:
    shared = sorted(set(a.user_chains) & set(b.user_chains))
    user_rows = []
    js: list[float] = []
    for uid in shared:
        ua = a.user_chains[uid][:top_n]
        ub = b.user_chains[uid][:top_n]
        if not ua and not ub:
            # Both samples have no chain for this user — uninformative;
            # would push Jaccard toward 1.0 by the empty-set convention and
            # bias the mean upward.
            continue
        j = jaccard(ua, ub)
        js.append(j)
        user_rows.append({
            "user_id": uid,
            "len_a": len(ua),
            "len_b": len(ub),
            "common_prefix_length": common_prefix_length(ua, ub),
            "jaccard": round(j, 4),
            "drift": round(1.0 - j, 4),
        })
    only_a = sorted(set(a.user_chains) - set(b.user_chains))
    only_b = sorted(set(b.user_chains) - set(a.user_chains))
    return {
        "sample_a": a.label,
        "sample_b": b.label,
        "top_n": top_n,
        "shared_users": len(shared),
        "users_compared": len(user_rows),
        "users_only_in_a": len(only_a),
        "users_only_in_b": len(only_b),
        "mean_jaccard": round(sum(js) / len(js), 4) if js else None,
        "median_jaccard": (
            round(sorted(js)[len(js) // 2], 4) if js else None
        ),
        "users_with_zero_drift": sum(1 for j in js if j >= 0.999),
        "users_above_0_7": sum(1 for j in js if j >= 0.70),
        "users_below_0_4": sum(1 for j in js if j < 0.40),
        "per_user": user_rows,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--raw-csv", required=True, nargs="+", metavar="LABEL=DIR",
        help="≥2 labelled raw CSV directories "
             "(e.g. 0506=data/dsk8k_24h_0506/raw 0507=data/dsk8k_24h_0507/raw)",
    )
    p.add_argument("--branch-threshold", type=float, default=0.45,
                   help="default 0.45 (recommended for production); "
                        "0.95 for strict closure")
    p.add_argument("--coverage-threshold", type=float, default=0.05)
    p.add_argument("--block-size", type=int, default=128)
    p.add_argument(
        "--top-n", type=int, default=20,
        help="Number of leading blocks of each chain to compare (default: 20)",
    )
    p.add_argument(
        "--output", required=True, type=Path,
        help="Path for chain_stability_report.json",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    try:
        samples = parse_sample_specs(args.raw_csv)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    print(
        f"Building chains for {len(samples)} samples  "
        f"(branch_thr={args.branch_threshold}, "
        f"cov_thr={args.coverage_threshold}, block={args.block_size})",
        flush=True,
    )
    for s in samples:
        try:
            s.build(args.branch_threshold, args.coverage_threshold, args.block_size)
        except FileNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        print(
            f"  {s.label:<8} requests={s.total_requests:>7,} "
            f"users={s.total_users:>4}  empty={s.empty_prompts:>4}  "
            f"global_chain_len={len(s.global_chain_keys):>4}  "
            f"({s.build_seconds:.1f}s, {s.csv_dir})",
            flush=True,
        )

    # ---- Pairwise comparisons ----
    global_pairs = []
    user_pairs = []
    global_jaccards: list[float] = []
    user_jaccard_means: list[float] = []

    for a, b in combinations(samples, 2):
        gc = compare_global_chain(a, b, args.top_n)
        pu = compare_per_user(a, b, args.top_n)
        global_pairs.append(gc)
        user_pairs.append(pu)
        global_jaccards.append(gc["jaccard"])
        if pu["mean_jaccard"] is not None:
            user_jaccard_means.append(pu["mean_jaccard"])

    overall_global_mean = (
        round(sum(global_jaccards) / len(global_jaccards), 4)
        if global_jaccards else None
    )
    overall_user_mean = (
        round(sum(user_jaccard_means) / len(user_jaccard_means), 4)
        if user_jaccard_means else None
    )
    overall_tier = (
        stability_tier(overall_user_mean) if overall_user_mean is not None else None
    )

    # ---- Output ----
    report = {
        "params": {
            "branch_threshold": args.branch_threshold,
            "coverage_threshold": args.coverage_threshold,
            "block_size": args.block_size,
            "top_n": args.top_n,
            "samples": [
                {
                    "label": s.label,
                    "csv_dir": str(s.csv_dir),
                    "total_requests": s.total_requests,
                    "total_users": s.total_users,
                    "empty_prompts": s.empty_prompts,
                    "global_chain_length": len(s.global_chain_keys),
                    "build_seconds": round(s.build_seconds, 2),
                }
                for s in samples
            ],
        },
        "summary": {
            "n_samples": len(samples),
            "n_pairs": len(global_pairs),
            "global_chain_mean_jaccard": overall_global_mean,
            "per_user_chain_mean_jaccard": overall_user_mean,
            "stability_tier": overall_tier,
            "tier_definition": {
                "high":         ">= 0.95   (static pin viable)",
                "slow_drift":   "0.70-0.95 (static pin + weekly refresh)",
                "medium_drift": "0.40-0.70 (dynamic admit/evict required)",
                "unstable":     "< 0.40    (chain-based optimization not viable)",
            },
            "note": (
                "Tier thresholds come from docs/3step_validation_plan.md §2.2/1.3 "
                "(originally specified for 7-day Jaccard). The same bands are applied "
                "to whatever windows the caller passed in; interpretation should "
                "consider the actual time spans of the input samples."
            ),
        },
        "global_chain_pairs": global_pairs,
        "per_user_chain_pairs": user_pairs,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # ---- Console summary ----
    print("\n=== Pairwise global-chain Jaccard (top-{}) ===".format(args.top_n),
          flush=True)
    print(f"  {'pair':<20} {'jaccard':>8} {'cpl':>5} {'len_a':>6} {'len_b':>6}",
          flush=True)
    print(f"  {'-'*20} {'-'*8} {'-'*5} {'-'*6} {'-'*6}", flush=True)
    for gc in global_pairs:
        pair = f"{gc['sample_a']} vs {gc['sample_b']}"
        print(
            f"  {pair:<20} {gc['jaccard']:>8.4f} {gc['common_prefix_length']:>5} "
            f"{gc['len_a']:>6} {gc['len_b']:>6}",
            flush=True,
        )

    print("\n=== Pairwise per-user-chain Jaccard ===", flush=True)
    print(
        f"  {'pair':<20} {'shared':>7} {'compared':>9} {'mean_j':>7} {'≥0.7':>5} {'<0.4':>5}",
        flush=True,
    )
    print(f"  {'-'*20} {'-'*7} {'-'*9} {'-'*7} {'-'*5} {'-'*5}", flush=True)
    for pu in user_pairs:
        pair = f"{pu['sample_a']} vs {pu['sample_b']}"
        mj = pu["mean_jaccard"]
        mj_disp = f"{mj:.4f}" if mj is not None else "  n/a"
        print(
            f"  {pair:<20} {pu['shared_users']:>7} {pu['users_compared']:>9} "
            f"{mj_disp:>7} {pu['users_above_0_7']:>5} {pu['users_below_0_4']:>5}",
            flush=True,
        )

    print("\n=== Stability summary ===", flush=True)
    print(f"  global-chain mean Jaccard   : {overall_global_mean}", flush=True)
    print(f"  per-user-chain mean Jaccard : {overall_user_mean}", flush=True)
    print(f"  stability tier              : {overall_tier}", flush=True)
    print(f"\n  output → {args.output}", flush=True)


if __name__ == "__main__":
    main()
