#!/usr/bin/env python3
"""Capacity sweep analysis: compare LRU / TTL-LRU / TwoQueueTTL across cache sizes.

Usage
-----
    # Basic: sweep a few capacities, print table
    python scripts/analyze_capacity_sweep.py --trace data/my_trace.csv

    # Full run: sweep + write CSV + generate plots
    python scripts/analyze_capacity_sweep.py \
        --trace data/my_trace.csv \
        --capacities 100 500 1000 2000 5000 10000 \
        --base-ttl 46 --extended-ttl 255 --promotion-threshold 2 \
        --output-dir outputs/sweep \
        --plot

    # Quick sanity check on toy trace
    python scripts/analyze_capacity_sweep.py \
        --trace tests/fixtures/toy_trace.csv \
        --capacities 2 3 4 6 8 \
        --base-ttl 10 --extended-ttl 100 \
        --output-dir outputs/toy_sweep \
        --plot
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.config import SimConfig, TTLLRUConfig, TwoQueueTTLConfig
from sim.io.raw_trace_loader import load_raw_trace
from sim.io.trace_loader import load_trace
from sim.metrics.collector import MetricsSnapshot, compute_gap_closed_ratios
from sim.runner import SimulationRunner


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Capacity sweep: hit rate vs cache size for all three policies",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--trace", required=True, help="Path to trace file (.csv or .jsonl)")
    p.add_argument(
        "--capacities", type=int, nargs="+",
        default=[100, 500, 1000, 2000, 5000, 10000],
        help="Cache capacities (blocks) to sweep",
    )
    p.add_argument("--block-size", type=int, default=128, help="Tokens per block")
    p.add_argument("--ttl", type=float, default=3600.0, help="TTL-LRU TTL (s)")
    p.add_argument("--base-ttl", type=float, default=46.0, help="TwoQueueTTL base TTL (s)")
    p.add_argument("--extended-ttl", type=float, default=255.0, help="TwoQueueTTL extended TTL (s)")
    p.add_argument("--promotion-threshold", type=int, default=2, help="TwoQueueTTL promotion threshold")
    p.add_argument("--protected-ratio", type=float, default=0.30, help="TwoQueueTTL protected queue ratio")
    p.add_argument("--output-dir", default="outputs/sweep", help="Directory for output files")
    p.add_argument("--plot", action="store_true", help="Generate matplotlib plots")
    p.add_argument("--raw", action="store_true",
                   help="Input CSV has raw_prompt column (request_id,user_id,raw_prompt,timestamp)")
    p.add_argument("--infinite-cache", action="store_true",
                   help="Include S0 (Infinite Cache) to compute ideal_hit_rate and gap_closed_ratio")
    p.add_argument("--belady", action="store_true",
                   help="Include S2 (Belady Oracle) to compute algorithmic upper bound "
                        "(slow: O(C) per eviction)")
    p.add_argument("--registry", default=None,
                   help="Path to offline block registry file for two_queue_ttl (S7)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Core sweep
# ---------------------------------------------------------------------------

def run_sweep(
    trace,
    capacities,
    block_size: int,
    ttl: float,
    base_ttl: float,
    extended_ttl: float,
    promotion_threshold: int,
    protected_ratio: float,
    include_infinite: bool = False,
    include_belady: bool = False,
    registry_path: str | None = None,
) -> list[dict]:
    """Run all configured policies at each capacity. Returns list of result dicts."""
    results = []
    tq_cfg = TwoQueueTTLConfig(
        base_ttl=base_ttl,
        extended_ttl=extended_ttl,
        promotion_threshold=promotion_threshold,
        protected_ratio=protected_ratio,
    )

    policy_count = 3 + int(include_infinite) + int(include_belady)
    total = len(capacities) * policy_count
    done = 0

    for cap in sorted(capacities):
        configs = []
        if include_infinite:
            configs.append(SimConfig(cache_capacity=cap, block_size=block_size,
                                     policy="infinite_cache"))
        configs.append(SimConfig(cache_capacity=cap, block_size=block_size, policy="lru"))
        configs.append(SimConfig(cache_capacity=cap, block_size=block_size, policy="ttl_lru",
                                 ttl_lru=TTLLRUConfig(ttl=ttl)))
        configs.append(SimConfig(cache_capacity=cap, block_size=block_size,
                                 policy="two_queue_ttl", two_queue_ttl=tq_cfg,
                                 registry_path=registry_path))
        if include_belady:
            configs.append(SimConfig(cache_capacity=cap, block_size=block_size,
                                     policy="belady"))

        snapshots = SimulationRunner.compare(trace, configs)

        # Compute gap_closed_ratio using infinite_cache and lru from this capacity
        compute_gap_closed_ratios(snapshots)

        for s in snapshots:
            done += 1
            gcr = f"  gap={s.gap_closed_ratio:.3f}" if s.gap_closed_ratio is not None else ""
            print(f"  [{done}/{total}] capacity={cap:>8}  policy={s.policy_name:<16}"
                  f"  hit_rate={s.prefix_block_hit_rate:.4f}{gcr}", flush=True)
            row = {"capacity": cap}
            row.update(s.to_dict())
            results.append(row)
    return results


# ---------------------------------------------------------------------------
# Output: CSV table
# ---------------------------------------------------------------------------

def write_csv(results: list[dict], output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "capacity_sweep.csv")
    if not results:
        return path
    fieldnames = list(results[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    return path


# ---------------------------------------------------------------------------
# Output: text comparison table
# ---------------------------------------------------------------------------

def print_summary_table(results: list[dict]) -> None:
    # Discover which policies are present in the results
    all_policies = ["infinite_cache", "lru", "belady", "ttl_lru", "two_queue_ttl"]
    policies_in_results = {r["policy"] for r in results}
    policies = [p for p in all_policies if p in policies_in_results]
    cap_set = sorted({r["capacity"] for r in results})
    col = max(14, max((len(p) for p in policies), default=0) + 2)

    col_w = 14 + col * len(policies)
    print("\n" + "=" * col_w)
    print(f"  prefix_block_hit_rate  (higher is better)")
    print(f"{'capacity':>12}" + "".join(f"{p:>{col}}" for p in policies))
    print("-" * col_w)
    for cap in cap_set:
        row_data = {r["policy"]: r["prefix_block_hit_rate"]
                    for r in results if r["capacity"] == cap}
        print(f"{cap:>12}" + "".join(
            f"{row_data.get(p, float('nan')):>{col}.4f}" for p in policies))
    print("=" * col_w + "\n")

    # gap_closed_ratio (only if infinite_cache is present)
    if "infinite_cache" in policies_in_results and "lru" in policies_in_results:
        print("=" * col_w)
        print(f"  gap_closed_ratio = (hit_rate - lru) / (infinite - lru)  (higher is better)")
        print(f"{'capacity':>12}" + "".join(f"{p:>{col}}" for p in policies))
        print("-" * col_w)
        for cap in cap_set:
            row_data = {r["policy"]: r.get("gap_closed_ratio")
                        for r in results if r["capacity"] == cap}
            cells = []
            for p in policies:
                v = row_data.get(p)
                cells.append(f"{v:{col}.4f}" if v is not None else f"{'—':>{col}}")
            print(f"{cap:>12}" + "".join(cells))
        print("=" * col_w + "\n")

    print("=" * col_w)
    print(f"  hot_prefix_eviction_count  (lower is better)")
    print(f"{'capacity':>12}" + "".join(f"{p:>{col}}" for p in policies))
    print("-" * col_w)
    for cap in cap_set:
        row_data = {r["policy"]: r["hot_prefix_eviction_count"]
                    for r in results if r["capacity"] == cap}
        print(f"{cap:>12}" + "".join(
            f"{row_data.get(p, -1):>{col}d}" for p in policies))
    print("=" * col_w + "\n")

    print("=" * col_w)
    print(f"  protected_pollution_rate  (lower is better, TwoQueueTTL only)")
    print(f"{'capacity':>12}" + "".join(f"{p:>{col}}" for p in policies))
    print("-" * col_w)
    for cap in cap_set:
        row_data = {r["policy"]: r["protected_pollution_rate"]
                    for r in results if r["capacity"] == cap}
        print(f"{cap:>12}" + "".join(
            f"{row_data.get(p, float('nan')):>{col}.4f}" for p in policies))
    print("=" * col_w + "\n")


# ---------------------------------------------------------------------------
# Output: matplotlib plots
# ---------------------------------------------------------------------------

def generate_plots(results: list[dict], output_dir: str, trace_path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    policies = ["lru", "ttl_lru", "two_queue_ttl"]
    colors = {"lru": "#4878cf", "ttl_lru": "#6acc65", "two_queue_ttl": "#d65f5f"}
    labels = {"lru": "LRU", "ttl_lru": "TTL-LRU", "two_queue_ttl": "Two-Queue TTL"}
    cap_set = sorted({r["capacity"] for r in results})

    def get_series(metric):
        out = {}
        for p in policies:
            out[p] = [r[metric] for r in results
                      if r["policy"] == p and r["capacity"] in cap_set]
        return out

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle(f"Phase 1 Capacity Sweep\n{os.path.basename(trace_path)}", fontsize=13)

    # ── Plot 1: Hit rate vs capacity ──────────────────────────────────────
    ax = axes[0, 0]
    series = get_series("prefix_block_hit_rate")
    for p in policies:
        ax.plot(cap_set, series[p], marker="o", label=labels[p], color=colors[p])
    ax.set_xlabel("Cache Capacity (blocks)")
    ax.set_ylabel("Prefix Block Hit Rate")
    ax.set_title("Hit Rate vs Capacity")
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.set_xscale("log" if max(cap_set) / max(min(cap_set), 1) > 10 else "linear")

    # ── Plot 2: Hit rate gain of TwoQueueTTL over LRU ───────────────────
    ax = axes[0, 1]
    lru_series = get_series("prefix_block_hit_rate")["lru"]
    tq_series  = get_series("prefix_block_hit_rate")["two_queue_ttl"]
    gain = [tq - lru for tq, lru in zip(tq_series, lru_series)]
    ax.bar(range(len(cap_set)), gain, color=colors["two_queue_ttl"], alpha=0.8)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_xticks(range(len(cap_set)))
    ax.set_xticklabels([str(c) for c in cap_set], rotation=30, ha="right")
    ax.set_xlabel("Cache Capacity (blocks)")
    ax.set_ylabel("Hit Rate Gain (TwoQueueTTL − LRU)")
    ax.set_title("TwoQueueTTL Gain over LRU")
    ax.grid(True, axis="y", alpha=0.3)

    # ── Plot 3: Hot-prefix eviction count ────────────────────────────────
    ax = axes[1, 0]
    series = get_series("hot_prefix_eviction_count")
    for p in policies:
        ax.plot(cap_set, series[p], marker="s", label=labels[p], color=colors[p])
    ax.set_xlabel("Cache Capacity (blocks)")
    ax.set_ylabel("Hot-Prefix Evictions (hit_count ≥ 2)")
    ax.set_title("Hot-Prefix Eviction Count\n(lower = better quality)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.set_xscale("log" if max(cap_set) / max(min(cap_set), 1) > 10 else "linear")

    # ── Plot 4: Protected pollution rate (TwoQueueTTL only) ──────────────
    ax = axes[1, 1]
    tq_cap = [r["capacity"] for r in results if r["policy"] == "two_queue_ttl"]
    tq_poll = [r["protected_pollution_rate"] for r in results if r["policy"] == "two_queue_ttl"]
    ax.plot(tq_cap, tq_poll, marker="^", color=colors["two_queue_ttl"],
            label="Two-Queue TTL")
    ax.axhline(0.3, color="red", linestyle="--", linewidth=0.8, label="30% warning line")
    ax.set_xlabel("Cache Capacity (blocks)")
    ax.set_ylabel("Protected Pollution Rate")
    ax.set_title("Protected Queue Pollution Rate\n(TwoQueueTTL: lower = better)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.set_ylim(0, 1.05)
    ax.set_xscale("log" if max(cap_set) / max(min(cap_set), 1) > 10 else "linear")

    plt.tight_layout()
    os.makedirs(output_dir, exist_ok=True)
    plot_path = os.path.join(output_dir, "capacity_sweep.png")
    fig.savefig(plot_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Plot saved: {plot_path}")

    # ── Plot 5: evicted_before_next_hit ──────────────────────────────────
    fig2, ax2 = plt.subplots(figsize=(8, 5))
    series = get_series("evicted_before_next_hit_count")
    for p in policies:
        ax2.plot(cap_set, series[p], marker="o", label=labels[p], color=colors[p])
    ax2.set_xlabel("Cache Capacity (blocks)")
    ax2.set_ylabel("Evicted-Before-Next-Hit Count")
    ax2.set_title("Blocks Evicted That Were Later Needed\n(lower = smarter eviction)")
    ax2.legend()
    ax2.grid(True, alpha=0.3)
    ax2.set_xscale("log" if max(cap_set) / max(min(cap_set), 1) > 10 else "linear")
    plot2_path = os.path.join(output_dir, "evicted_before_next_hit.png")
    fig2.savefig(plot2_path, dpi=150, bbox_inches="tight")
    plt.close(fig2)
    print(f"Plot saved: {plot2_path}")


# ---------------------------------------------------------------------------
# Trace statistics (pre-flight)
# ---------------------------------------------------------------------------

def print_trace_stats(trace) -> None:
    from collections import Counter
    n = len(trace)
    block_counts = [len(r.hash_ids) for r in trace]
    unique_users = len({r.user_id for r in trace})
    unique_models = len({r.model_id for r in trace})
    unique_blocks = len({h for r in trace for h in r.hash_ids})
    total_blocks = sum(block_counts)
    t_min = min(r.timestamp for r in trace)
    t_max = max(r.timestamp for r in trace)

    print("\n── Trace Statistics ─────────────────────────────────")
    print(f"  requests             : {n:,}")
    print(f"  time span            : {t_max - t_min:.1f} s  ({t_min:.1f} → {t_max:.1f})")
    print(f"  unique users         : {unique_users:,}")
    print(f"  unique models        : {unique_models:,}")
    print(f"  total block refs     : {total_blocks:,}")
    print(f"  unique block hashes  : {unique_blocks:,}")
    print(f"  avg blocks/request   : {total_blocks/n:.1f}")
    print(f"  max blocks/request   : {max(block_counts):,}")
    print(f"  reuse potential      : {total_blocks/unique_blocks:.2f}x  (total/unique)")
    print("─────────────────────────────────────────────────────\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    print(f"Loading trace: {args.trace}")
    if args.raw:
        trace = load_raw_trace(args.trace, block_size=args.block_size)
    else:
        trace = load_trace(args.trace)
    print(f"  {len(trace):,} records loaded")
    print_trace_stats(trace)

    print(f"Running capacity sweep: {args.capacities}")
    results = run_sweep(
        trace=trace,
        capacities=args.capacities,
        block_size=args.block_size,
        ttl=args.ttl,
        base_ttl=args.base_ttl,
        extended_ttl=args.extended_ttl,
        promotion_threshold=args.promotion_threshold,
        protected_ratio=args.protected_ratio,
        include_infinite=args.infinite_cache,
        include_belady=args.belady,
        registry_path=args.registry,
    )

    print_summary_table(results)

    csv_path = write_csv(results, args.output_dir)
    print(f"CSV written: {csv_path}")

    summary_path = os.path.join(args.output_dir, "sweep_results.json")
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"JSON written: {summary_path}")

    if args.plot:
        generate_plots(results, args.output_dir, args.trace)


if __name__ == "__main__":
    main()
