#!/usr/bin/env python3
"""Multi-tenant trace characterization for KV cache research.

Answers the key questions before running simulation experiments:
  1. User distribution: how skewed is the request distribution?
  2. Heavy vs light user segmentation: where is the 80/20 line?
  3. Reuse time distribution: what TTL values are appropriate?
  4. Working set size: what capacity range should we sweep?
  5. Prefix sharing structure: do heavy users share prefixes?
  6. Session structure: how many turns per session, inter-turn gaps?

Usage
-----
  python scripts/analyze_multitenant_trace.py \
      --trace data/qwen32b_processed.csv \
      --heavy-pct 10 \
      --output-dir outputs/qwen32b/trace_analysis
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.io.trace_loader import load_trace, precompute_prefix_keys
from sim.core.future_index import build_future_index


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--trace", required=True)
    p.add_argument("--heavy-pct", type=float, default=10.0,
                   help="Top X%% of users by request count = heavy users")
    p.add_argument("--block-size", type=int, default=128)
    p.add_argument("--output-dir", default="outputs/trace_analysis")
    p.add_argument("--raw", action="store_true")
    return p.parse_args()


# ---------------------------------------------------------------------------
# 1. User distribution
# ---------------------------------------------------------------------------

def analyze_user_distribution(trace, heavy_pct: float) -> dict:
    req_per_user = Counter(r.user_id for r in trace)
    users_sorted = sorted(req_per_user.values(), reverse=True)
    total_users = len(users_sorted)
    total_reqs  = sum(users_sorted)

    # Identify heavy user threshold
    heavy_n = max(1, int(total_users * heavy_pct / 100))
    heavy_reqs = sum(users_sorted[:heavy_n])
    light_reqs = total_reqs - heavy_reqs

    print("\n── User Distribution ────────────────────────────────────────────")
    print(f"  total users            : {total_users:,}")
    print(f"  total requests         : {total_reqs:,}")
    print(f"  avg requests/user      : {total_reqs/total_users:.1f}")
    print(f"  max requests/user      : {users_sorted[0]:,}")
    print(f"  median requests/user   : {users_sorted[total_users//2]:,}")
    print()
    print(f"  Heavy users (top {heavy_pct:.0f}%)  : {heavy_n:,} users")
    print(f"    → {heavy_reqs:,} requests  ({100*heavy_reqs/total_reqs:.1f}% of total)")
    print(f"  Light users (rest)     : {total_users-heavy_n:,} users")
    print(f"    → {light_reqs:,} requests  ({100*light_reqs/total_reqs:.1f}% of total)")

    # Percentile breakdown
    print("\n  Request count percentiles (per user):")
    for pct in [50, 80, 90, 95, 99]:
        idx = min(int(total_users * (1 - pct/100)), total_users - 1)
        print(f"    p{pct:>2}  : {users_sorted[idx]:>6,} requests/user")
    print("─────────────────────────────────────────────────────────────────")

    heavy_user_ids = {uid for uid, cnt in req_per_user.most_common(heavy_n)}
    return {
        "total_users": total_users,
        "total_requests": total_reqs,
        "heavy_n": heavy_n,
        "heavy_pct": heavy_pct,
        "heavy_req_pct": 100 * heavy_reqs / total_reqs,
        "heavy_user_ids": heavy_user_ids,
        "req_per_user": req_per_user,
    }


# ---------------------------------------------------------------------------
# 2. Block-level statistics by user tier
# ---------------------------------------------------------------------------

def analyze_block_distribution(trace, heavy_user_ids: set) -> dict:
    heavy_blocks: set[str] = set()
    light_blocks: set[str] = set()
    heavy_total = 0
    light_total = 0

    for r in trace:
        if r.user_id in heavy_user_ids:
            heavy_blocks.update(r.hash_ids)
            heavy_total += len(r.hash_ids)
        else:
            light_blocks.update(r.hash_ids)
            light_total += len(r.hash_ids)

    shared = heavy_blocks & light_blocks
    total_unique = len(heavy_blocks | light_blocks)

    print("\n── Block Distribution by User Tier ──────────────────────────────")
    print("  Heavy users:")
    print(f"    unique blocks        : {len(heavy_blocks):,}")
    print(f"    total block refs     : {heavy_total:,}")
    print(f"    avg reuse (heavy)    : {heavy_total/max(len(heavy_blocks),1):.2f}x")
    print("  Light users:")
    print(f"    unique blocks        : {len(light_blocks):,}")
    print(f"    total block refs     : {light_total:,}")
    print(f"    avg reuse (light)    : {light_total/max(len(light_blocks),1):.2f}x")
    print(f"  Cross-tier shared blocks : {len(shared):,}  "
          f"({100*len(shared)/max(total_unique,1):.1f}% of all unique)")
    print(f"  Total unique blocks      : {total_unique:,}")

    # Capacity estimate: minimum cache to hold all heavy-user working set
    heavy_ws = len(heavy_blocks)
    print("\n  Working set estimates:")
    print(f"    Heavy users working set  : {heavy_ws:,} blocks  "
          f"({heavy_ws*128//1000:,}K tokens)")
    print(f"    Full working set         : {total_unique:,} blocks")
    print("─────────────────────────────────────────────────────────────────")

    return {
        "heavy_unique_blocks": len(heavy_blocks),
        "light_unique_blocks": len(light_blocks),
        "shared_blocks": len(shared),
        "total_unique": total_unique,
        "heavy_total_refs": heavy_total,
        "light_total_refs": light_total,
        "heavy_reuse_multiplier": heavy_total / max(len(heavy_blocks), 1),
        "light_reuse_multiplier": light_total / max(len(light_blocks), 1),
    }


# ---------------------------------------------------------------------------
# 3. Reuse time distribution
# ---------------------------------------------------------------------------

def analyze_reuse_times(trace, future_index: dict) -> dict:
    """Compute inter-access intervals for blocks that are accessed > once."""
    # Build access timeline per block using prefix_path_keys
    block_times: dict[str, list[float]] = defaultdict(list)
    for r in trace:
        keys = r.prefix_path_keys or []
        for k in keys:
            k_hex = k.hex() if isinstance(k, bytes) else k
            block_times[k_hex].append(r.timestamp)

    intervals: list[float] = []
    for times in block_times.values():
        if len(times) < 2:
            continue
        times_sorted = sorted(times)
        for i in range(1, len(times_sorted)):
            intervals.append(times_sorted[i] - times_sorted[i-1])

    if not intervals:
        print("\n  No reuse events found.")
        return {}

    intervals.sort()
    n = len(intervals)

    def pct(p):
        return intervals[min(int(n * p / 100), n - 1)]

    print("\n── Reuse Time Distribution ──────────────────────────────────────")
    print(f"  reuse events           : {n:,}")
    print("  Percentile    reuse_time_seconds  reuse_time_minutes")
    print(f"  {'─'*52}")
    for p in [10, 25, 50, 75, 80, 90, 95, 99]:
        v = pct(p)
        print(f"  p{p:<3}           {v:>18.1f}  {v/60:>18.1f}")
    print("─────────────────────────────────────────────────────────────────")
    print("\n  TTL recommendations (based on reuse time):")
    p80 = pct(80)
    p95 = pct(95)
    print(f"    base_ttl     = {p80:.0f}s  (p80 — protects 80% of reuse events)")
    print(f"    extended_ttl = {p95:.0f}s  (p95 — for high-frequency blocks)")
    print("─────────────────────────────────────────────────────────────────")

    return {
        "reuse_event_count": n,
        "p50": pct(50), "p80": pct(80), "p95": pct(95),
        "recommended_base_ttl": p80,
        "recommended_extended_ttl": p95,
    }


# ---------------------------------------------------------------------------
# 4. Session structure (inter-turn gap per user)
# ---------------------------------------------------------------------------

def analyze_session_structure(trace, heavy_user_ids: set) -> dict:
    user_timestamps: dict[str, list[float]] = defaultdict(list)
    for r in trace:
        user_timestamps[r.user_id].append(r.timestamp)

    heavy_gaps: list[float] = []
    light_gaps:  list[float] = []

    for uid, times in user_timestamps.items():
        if len(times) < 2:
            continue
        times_sorted = sorted(times)
        gaps = [times_sorted[i+1] - times_sorted[i]
                for i in range(len(times_sorted) - 1)]
        if uid in heavy_user_ids:
            heavy_gaps.extend(gaps)
        else:
            light_gaps.extend(gaps)

    def summarize(gaps: list[float], label: str) -> None:
        if not gaps:
            print(f"  {label}: no multi-turn sessions")
            return
        gaps.sort()
        n = len(gaps)
        def pct(p): return gaps[min(int(n*p/100), n-1)]
        print(f"  {label} inter-turn gap ({n:,} gaps):")
        for p in [50, 80, 95]:
            v = pct(p)
            print(f"    p{p}  : {v:>8.1f}s  ({v/60:.1f} min)")

    print("\n── Session Structure (inter-turn gaps) ──────────────────────────")
    summarize(heavy_gaps, "Heavy users")
    summarize(light_gaps, "Light users")
    print("─────────────────────────────────────────────────────────────────")

    heavy_gaps.sort() if heavy_gaps else None
    light_gaps.sort()  if light_gaps else None

    def safe_pct(gaps, p):
        if not gaps:
            return None
        n = len(gaps)
        return gaps[min(int(n*p/100), n-1)]

    return {
        "heavy_gap_p50": safe_pct(heavy_gaps, 50),
        "heavy_gap_p80": safe_pct(heavy_gaps, 80),
        "heavy_gap_p95": safe_pct(heavy_gaps, 95),
        "light_gap_p50": safe_pct(light_gaps, 50),
        "light_gap_p80": safe_pct(light_gaps, 80),
    }


# ---------------------------------------------------------------------------
# 5. Capacity sweep recommendations
# ---------------------------------------------------------------------------

def recommend_capacities(block_stats: dict, reuse_stats: dict, trace) -> list[int]:
    total_unique = block_stats["total_unique"]
    heavy_ws     = block_stats["heavy_unique_blocks"]
    time_span    = trace[-1].timestamp - trace[0].timestamp
    insertion_rate = total_unique / time_span if time_span > 0 else 1

    print("\n── Capacity Sweep Recommendations ───────────────────────────────")
    print(f"  heavy user working set   : {heavy_ws:,} blocks")
    print(f"  full working set         : {total_unique:,} blocks")
    print(f"  insertion rate           : {insertion_rate:.1f} blocks/s")

    # Suggest capacities spanning from "too small" to "near-optimal"
    anchors = [
        int(heavy_ws * 0.05),   # 5% of heavy WS — severely undersized
        int(heavy_ws * 0.20),   # 20% of heavy WS — small
        int(heavy_ws * 0.50),   # 50% of heavy WS — moderate
        int(heavy_ws * 1.00),   # 100% = full heavy WS
        int(heavy_ws * 2.00),   # 200% = heavy + some light
        total_unique,            # full WS
    ]
    # Round to nice numbers and deduplicate
    def round_cap(c):
        if c < 1000:
            return max(100, c)
        magnitude = 10 ** (len(str(c)) - 2)
        return max(1000, round(c / magnitude) * magnitude)

    caps = sorted(set(round_cap(a) for a in anchors if a > 0))
    print(f"\n  Recommended capacities   : {caps}")
    print("  Rationale:")
    for c, pct in zip(caps, ["~5% heavy WS", "~20% heavy WS", "~50% heavy WS",
                               "~100% heavy WS", "~200% heavy WS", "full WS"]):
        print(f"    {c:>8,} blocks  ({pct})")
    print("─────────────────────────────────────────────────────────────────")
    return caps


# ---------------------------------------------------------------------------
# 6. Print experiment command
# ---------------------------------------------------------------------------

def print_experiment_commands(args, reuse_stats: dict, caps: list[int]) -> None:
    base_ttl = reuse_stats.get("recommended_base_ttl", 138.0)
    ext_ttl  = reuse_stats.get("recommended_extended_ttl", 1612.0)
    caps_str = " ".join(str(c) for c in caps)

    print("\n── Suggested Experiment Commands ────────────────────────────────")
    print("\n  # Phase 2: Baseline capacity sweep")
    print("  python scripts/analyze_capacity_sweep.py \\")
    print(f"      --trace {args.trace} \\")
    print(f"      --capacities {caps_str} \\")
    print(f"      --base-ttl {base_ttl:.0f} \\")
    print(f"      --extended-ttl {ext_ttl:.0f} \\")
    print("      --infinite-cache \\")
    print(f"      --output-dir {args.output_dir}/phase2_baseline")

    print("\n  # Phase 3: Plan A+B1 (adaptive TTL + threshold sweep)")
    print("  python scripts/experiment_plan_ab.py \\")
    print(f"      --trace {args.trace} \\")
    print(f"      --capacities {caps_str} \\")
    print(f"      --probe-capacities {' '.join(str(c) for c in caps[1:4])} \\")
    print("      --thresholds 2 3 4 5 \\")
    print(f"      --base-ttl {base_ttl:.0f} \\")
    print("      --alpha 0.7 \\")
    print(f"      --output-dir {args.output_dir}/phase3_plan_ab")
    print("─────────────────────────────────────────────────────────────────\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    print(f"\nLoading trace: {args.trace}")
    if args.raw:
        from sim.io.raw_trace_loader import load_raw_trace
        trace = load_raw_trace(args.trace, block_size=args.block_size)
    else:
        trace = load_trace(args.trace)
    print(f"  {len(trace):,} records loaded")

    t_min = min(r.timestamp for r in trace)
    t_max = max(r.timestamp for r in trace)
    print(f"  time span: {t_max - t_min:.1f}s  ({t_min:.1f} → {t_max:.1f})")
    print(f"  avg blocks/request: {sum(len(r.hash_ids) for r in trace)/len(trace):.1f}")

    precompute_prefix_keys(trace)

    import time
    print("\n  Building future access index …", flush=True)
    t0 = time.time()
    future_index = build_future_index(trace)
    print(f"  Future index ready ({time.time()-t0:.1f}s, "
          f"{len(future_index):,} unique block keys)")

    # Infinite hit rate
    total_refs = sum(len(r.hash_ids) for r in trace)
    unique_keys = len(future_index)
    infinite_hit_rate = 1 - unique_keys / total_refs if total_refs > 0 else 0
    print(f"\n  Infinite cache hit rate  : {infinite_hit_rate:.4f}  "
          f"({infinite_hit_rate*100:.1f}%)")
    print(f"  Reuse multiplier         : {total_refs/max(unique_keys,1):.2f}x")

    # Analyses
    user_stats  = analyze_user_distribution(trace, args.heavy_pct)
    block_stats = analyze_block_distribution(trace, user_stats["heavy_user_ids"])
    reuse_stats = analyze_reuse_times(trace, future_index)
    session_stats = analyze_session_structure(trace, user_stats["heavy_user_ids"])
    caps = recommend_capacities(block_stats, reuse_stats, trace)
    print_experiment_commands(args, reuse_stats, caps)

    # Save summary
    os.makedirs(args.output_dir, exist_ok=True)
    summary = {
        "trace": args.trace,
        "total_records": len(trace),
        "time_span_s": t_max - t_min,
        "infinite_hit_rate": infinite_hit_rate,
        "reuse_multiplier": total_refs / max(unique_keys, 1),
        "user": {k: v for k, v in user_stats.items() if k != "heavy_user_ids"},
        "blocks": block_stats,
        "reuse_times": reuse_stats,
        "session": session_stats,
        "recommended_capacities": caps,
    }
    out = os.path.join(args.output_dir, "trace_summary.json")
    with open(out, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"Summary saved: {out}\n")


if __name__ == "__main__":
    main()
