#!/usr/bin/env python3
"""Step 1.3 — Cross-window chain stability analyzer.

Quantifies how prefix-path chains drift across time windows. Consumes the
per-user-chain JSON artifacts produced by Step 1.2 and emits a stability
report covering:

  * Pairwise top-N Jaccard between samples (global chain block-set)
  * Per-user chain Jaccard across samples that share a user
  * Stability tier per the thresholds defined in
    docs/3step_validation_plan.md §2.2/1.3

Reusable across models — input is a list of LABEL=PATH pairs naming Step 1.2
JSON outputs, and there is no dependency on any specific tokenizer or model.
Offline-safe.

Skeleton status (2026-05-07): structure + core computations are in place.
Threshold-tier wording matches the plan; awaiting the three dsk8k_* samples
to validate end-to-end. See `docs/3step_validation_plan.md` §2.2/1.3.

Usage
-----
  python scripts/chain_stability_analyzer.py \\
      --input  2h=outputs/dsk8k_2h_5k/per_user_chains.json \\
               24h=outputs/dsk8k_24h_10k/per_user_chains.json \\
               2d=outputs/dsk8k_2d_10k/per_user_chains.json \\
      --top-n  20 \\
      --output outputs/dsk8k_step1_3/chain_stability_report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from itertools import combinations
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Sample loading
# ---------------------------------------------------------------------------

class Sample:
    """One Step 1.2 JSON output, indexed for stability comparison."""

    def __init__(self, label: str, path: Path, payload: dict):
        self.label = label
        self.path = path
        self.payload = payload

        gc = payload.get("global_chain") or {}
        # Ordered list of prefix_path_key hex strings (one per block).
        self.global_chain_keys: list[str] = [
            entry["prefix_path_key"] for entry in gc.get("lcp_content", [])
        ]

        # user_id -> ordered list of hex prefix_path_keys
        self.user_chains: dict[str, list[str]] = {}
        for u in payload.get("users", []):
            keys = [entry["prefix_path_key"] for entry in u.get("lcp_content", [])]
            self.user_chains[u["user_id"]] = keys

        self.total_requests: int = (payload.get("stats") or {}).get("total_requests", 0)
        self.total_users: int = (payload.get("stats") or {}).get("total_users", 0)
        # 1.2 analyzer params (branch_threshold / coverage_threshold / block_size).
        # Required for the cross-sample consistency check — chain comparison is
        # only meaningful when all samples were analyzed under identical params.
        self.analyzer_params: dict = payload.get("params") or {}

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"Sample({self.label}, requests={self.total_requests}, "
            f"users={self.total_users}, global_chain_len={len(self.global_chain_keys)})"
        )


def load_samples(spec_pairs: list[str]) -> list[Sample]:
    """Parse `LABEL=PATH` pairs and return loaded samples."""
    samples: list[Sample] = []
    seen_labels: set[str] = set()
    for spec in spec_pairs:
        if "=" not in spec:
            raise ValueError(
                f"--input value {spec!r} is not LABEL=PATH (e.g. 2h=outputs/.../per_user_chains.json)"
            )
        label, _, raw_path = spec.partition("=")
        label = label.strip()
        path = Path(raw_path.strip())
        if not label:
            raise ValueError(f"empty label in --input value {spec!r}")
        if label in seen_labels:
            raise ValueError(f"duplicate sample label: {label!r}")
        seen_labels.add(label)
        if not path.exists():
            raise FileNotFoundError(f"sample {label!r}: {path} does not exist")
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        samples.append(Sample(label, path, payload))
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
    """Maps mean Jaccard to the four tiers defined in plan §2.2/1.3.

    The plan thresholds are oriented around 7-day stability; this function
    applies the same cutoffs to whatever windows the caller supplied. The
    tier *string* therefore reflects the band; whether the band is interpreted
    as "7-day stability" depends on the input set passed in.
    """
    if mean_jaccard >= 0.95:
        return "high"           # 静态 pin 可行
    if mean_jaccard >= 0.70:
        return "slow_drift"     # 静态 pin + 周级更新
    if mean_jaccard >= 0.40:
        return "medium_drift"   # 必须动态准入/退出
    return "unstable"           # chain 优化方向不可行


# ---------------------------------------------------------------------------
# Cross-sample sanity
# ---------------------------------------------------------------------------

PARAM_KEYS = ("branch_threshold", "coverage_threshold", "block_size")


def check_params_consistency(samples: list["Sample"]) -> list[str]:
    """Return human-readable mismatch lines, or [] if all samples agree.

    Comparing chains across samples that were analyzed with different 1.2
    params (e.g. one with branch_threshold=0.40 and another with 0.45) is
    methodologically invalid: any chain-length / Jaccard difference can be
    attributed either to time drift or to the threshold change, and the two
    cannot be separated. The caller must either re-run 1.2 with matched
    params, or explicitly opt in via --allow-mismatched-params.
    """
    base = samples[0]
    base_p = {k: base.analyzer_params.get(k) for k in PARAM_KEYS}
    msgs: list[str] = []
    for s in samples[1:]:
        for k in PARAM_KEYS:
            v = s.analyzer_params.get(k)
            if v != base_p[k]:
                msgs.append(
                    f"  {s.label}.{k} = {v!r}  vs  {base.label}.{k} = {base_p[k]!r}"
                )
    return msgs


# ---------------------------------------------------------------------------
# Pairwise comparisons
# ---------------------------------------------------------------------------

def compare_global_chain(
    a: Sample, b: Sample, top_n: int,
) -> dict:
    """Compare the two samples' global chain block sets, capped at top_n."""
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


def compare_per_user(
    a: Sample, b: Sample, top_n: int,
) -> dict:
    """Per-user chain Jaccard for users present in both samples."""
    shared = sorted(set(a.user_chains) & set(b.user_chains))
    user_rows = []
    js: list[float] = []
    for uid in shared:
        ua = a.user_chains[uid][:top_n]
        ub = b.user_chains[uid][:top_n]
        # Skip users with no chain in either sample — these are uninformative
        # (Jaccard would be the empty-set convention 1.0 and bias the mean).
        if not ua and not ub:
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
        "--input", required=True, nargs="+", metavar="LABEL=PATH",
        help="≥2 Step 1.2 JSON outputs, each tagged with a label "
             "(e.g. 2h=outputs/.../per_user_chains.json)",
    )
    p.add_argument(
        "--top-n", type=int, default=20,
        help="Number of leading blocks of each chain to compare (default: 20)",
    )
    p.add_argument(
        "--output", required=True, type=Path,
        help="Path for chain_stability_report.json",
    )
    p.add_argument(
        "--allow-mismatched-params", action="store_true",
        help="Skip the cross-sample 1.2-params consistency check. Use only "
             "when intentionally comparing different 1.2 configurations; "
             "results will not be apples-to-apples.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    try:
        samples = load_samples(args.input)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(samples)} samples:", flush=True)
    for s in samples:
        bp = s.analyzer_params.get("branch_threshold")
        cp = s.analyzer_params.get("coverage_threshold")
        bs = s.analyzer_params.get("block_size")
        print(
            f"  {s.label:<8} requests={s.total_requests:>7,} "
            f"users={s.total_users:>4}  global_chain_len={len(s.global_chain_keys)}  "
            f"branch_thr={bp} cov_thr={cp} block={bs}",
            flush=True,
        )
        print(f"           ({s.path})", flush=True)

    # ---- Cross-sample params consistency ----
    mismatches = check_params_consistency(samples)
    if mismatches:
        header = (
            "Sample 1.2 analyzer params differ across inputs — chain "
            "comparison would be methodologically invalid:"
        )
        if args.allow_mismatched_params:
            print(f"\nwarning: {header}", file=sys.stderr)
            for m in mismatches:
                print(m, file=sys.stderr)
            print(
                "(continuing because --allow-mismatched-params was set)\n",
                file=sys.stderr,
            )
        else:
            print(f"\nerror: {header}", file=sys.stderr)
            for m in mismatches:
                print(m, file=sys.stderr)
            print(
                "\nRe-run Step 1.2 on all samples with identical params, or "
                "pass --allow-mismatched-params to override.",
                file=sys.stderr,
            )
            sys.exit(1)

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
            "top_n": args.top_n,
            "samples": [
                {
                    "label": s.label,
                    "path": str(s.path),
                    "total_requests": s.total_requests,
                    "total_users": s.total_users,
                    "global_chain_length": len(s.global_chain_keys),
                    "analyzer_params": {
                        k: s.analyzer_params.get(k) for k in PARAM_KEYS
                    },
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
