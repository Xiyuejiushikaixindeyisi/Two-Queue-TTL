#!/usr/bin/env python3
"""Experiment Plan A + B1: Capacity-Adaptive TTL & promotion_threshold sweep.

Plan A — Capacity-Adaptive TTL
  TTL is set per-capacity rather than a fixed global constant:
    insertion_rate  = unique_block_keys / trace_time_span   (from future_index)
    cache_lifetime  = capacity / insertion_rate
    adaptive_ttl    = max(base_ttl, alpha × cache_lifetime)
  alpha=0.7 keeps adaptive_ttl slightly below the natural eviction horizon so
  TTL can still identify truly dead sessions without firing on active ones.

Plan B1 — Promotion threshold sweep
  Test promotion_threshold ∈ {2, 3, 4, 5} with adaptive TTL to reduce the
  Protected queue pollution observed at small capacities (0.9772 @ 5K).

Experiment order
  Part 1 — Baseline (original params: TTL=46s, threshold=2) for reference
  Part 2 — Plan A only: per-capacity adaptive TTL, threshold=2
  Part 3 — Plan B1 + A: adaptive TTL × threshold sweep at 3 probe capacities
  Part 4 — Best A+B1 full sweep (all capacities, best threshold from Part 3)

Usage
-----
  # Minimal: same trace path as the Phase 1 sweep
  python scripts/experiment_plan_ab.py --trace data/trace.csv

  # Full options
  python scripts/experiment_plan_ab.py \\
      --trace data/trace.csv \\
      --capacities 5000 10000 26000 53500 100000 \\
      --probe-capacities 5000 26000 100000 \\
      --thresholds 2 3 4 5 \\
      --alpha 0.7 \\
      --base-ttl 46.0 \\
      --output-dir outputs/plan_ab \\
      --raw
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.config import SimConfig, TTLLRUConfig, TwoQueueTTLConfig
from sim.core.future_index import build_future_index
from sim.io.raw_trace_loader import load_raw_trace
from sim.io.trace_loader import load_trace, precompute_prefix_keys
from sim.metrics.collector import MetricsSnapshot, compute_gap_closed_ratios
from sim.runner import SimulationRunner


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Plan A+B1 experiment: adaptive TTL + promotion threshold sweep",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--trace", required=True)
    p.add_argument("--capacities", type=int, nargs="+",
                   default=[5000, 10000, 26000, 53500, 100000])
    p.add_argument("--probe-capacities", type=int, nargs="+",
                   default=[5000, 26000, 100000],
                   help="Subset of capacities used for threshold sweep (Part 3) — "
                        "keeps runtime manageable")
    p.add_argument("--thresholds", type=int, nargs="+", default=[2, 3, 4, 5],
                   help="promotion_threshold values to sweep in Part 3")
    p.add_argument("--alpha", type=float, default=0.7,
                   help="Fraction of cache_lifetime used as adaptive TTL upper bound")
    p.add_argument("--base-ttl", type=float, default=46.0,
                   help="Minimum TTL (floor for adaptive formula and baseline)")
    p.add_argument("--block-size", type=int, default=128)
    p.add_argument("--output-dir", default="outputs/plan_ab")
    p.add_argument("--raw", action="store_true",
                   help="Input is a raw-prompt CSV (request_id, user_id, raw_prompt, timestamp)")
    p.add_argument("--skip-baseline", action="store_true",
                   help="Skip Part 1 baseline sweep (use if Phase 1 results already available)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Adaptive TTL computation
# ---------------------------------------------------------------------------

def compute_adaptive_ttls(
    future_index: dict,
    trace,
    capacities: list[int],
    base_ttl: float,
    alpha: float,
) -> dict[int, float]:
    """Return {capacity: adaptive_ttl} for each requested capacity.

    insertion_rate estimate: unique_block_keys / trace_time_span
    This uses the count of unique prefix-path keys (from the already-built
    future_index) as the arrival rate of genuinely new blocks.

    For small capacities where alpha×cache_lifetime < base_ttl, the floor
    ensures TTL stays meaningful.
    """
    time_span = trace[-1].timestamp - trace[0].timestamp
    unique_blocks = len(future_index)
    insertion_rate = unique_blocks / time_span if time_span > 0 else 1.0

    result = {}
    print("\n── Adaptive TTL computation ─────────────────────────────────────")
    print(f"  unique block keys : {unique_blocks:,}")
    print(f"  trace time span   : {time_span:.1f} s")
    print(f"  insertion_rate    : {insertion_rate:.1f} blocks/s  (unique keys / time_span)")
    print(f"  alpha             : {alpha}")
    print(f"  base_ttl (floor)  : {base_ttl} s\n")
    print(f"  {'capacity':>10}  {'cache_lifetime':>16}  {'adaptive_ttl':>14}")
    print(f"  {'─'*10}  {'─'*16}  {'─'*14}")
    for cap in sorted(capacities):
        cache_lifetime = cap / insertion_rate
        adaptive_ttl = max(base_ttl, alpha * cache_lifetime)
        result[cap] = round(adaptive_ttl, 1)
        flag = " (= floor)" if adaptive_ttl == base_ttl else ""
        print(f"  {cap:>10,}  {cache_lifetime:>14.1f} s  {adaptive_ttl:>12.1f} s{flag}")
    print("─────────────────────────────────────────────────────────────────\n")
    return result


# ---------------------------------------------------------------------------
# Single-run helper
# ---------------------------------------------------------------------------

def run_one(
    cfg: SimConfig,
    trace,
    future_index: dict,
    label: str,
    run_num: int,
    total: int,
) -> MetricsSnapshot:
    print(
        f"  [{run_num:>3}/{total}]  cap={cfg.cache_capacity:>8,}  "
        f"policy={cfg.policy:<16}  {label:<30}  running …",
        end="", flush=True,
    )
    t0 = time.time()
    snap = SimulationRunner(cfg).run(trace, future_index=future_index)
    elapsed = time.time() - t0
    print(f"  hit={snap.prefix_block_hit_rate:.4f}  ({elapsed:.1f}s)", flush=True)
    return snap


# ---------------------------------------------------------------------------
# Part 1 — Baseline
# ---------------------------------------------------------------------------

def part1_baseline(
    trace, future_index, capacities, block_size, base_ttl, args
) -> list[dict]:
    """Original params: TTL=46s, threshold=2."""
    print("\n" + "═" * 70)
    print("  PART 1 — Baseline  (TTL=46s, threshold=2)")
    print("═" * 70)

    policies = ["infinite_cache", "lru", "ttl_lru", "two_queue_ttl"]
    total = len(capacities) * len(policies)
    run_num = 0
    all_results: list[dict] = []

    for cap in sorted(capacities):
        configs = [
            SimConfig(cache_capacity=cap, block_size=block_size, policy="infinite_cache"),
            SimConfig(cache_capacity=cap, block_size=block_size, policy="lru"),
            SimConfig(cache_capacity=cap, block_size=block_size, policy="ttl_lru",
                      ttl_lru=TTLLRUConfig(ttl=base_ttl)),
            SimConfig(cache_capacity=cap, block_size=block_size, policy="two_queue_ttl",
                      two_queue_ttl=TwoQueueTTLConfig(
                          base_ttl=base_ttl,
                          extended_ttl=base_ttl * (255 / 46),
                          promotion_threshold=2,
                      )),
        ]
        snaps = []
        for cfg in configs:
            run_num += 1
            snap = run_one(cfg, trace, future_index,
                           f"ttl={base_ttl:.0f}s thresh=2", run_num, total)
            snaps.append(snap)
        compute_gap_closed_ratios(snaps)
        for s in snaps:
            row = {"part": "baseline", "capacity": cap,
                   "label": f"ttl={base_ttl:.0f}s,thresh=2"}
            row.update(s.to_dict())
            all_results.append(row)

    _print_hit_table("Baseline", all_results, ["infinite_cache", "lru", "ttl_lru", "two_queue_ttl"])
    _print_gap_table("Baseline", all_results, ["infinite_cache", "lru", "ttl_lru", "two_queue_ttl"])
    _print_pollution_table("Baseline", all_results)
    return all_results


# ---------------------------------------------------------------------------
# Part 2 — Plan A: adaptive TTL, threshold=2
# ---------------------------------------------------------------------------

def part2_adaptive_ttl(
    trace, future_index, capacities, block_size, adaptive_ttls, base_ttl
) -> list[dict]:
    """Per-capacity adaptive TTL, promotion_threshold fixed at 2."""
    print("\n" + "═" * 70)
    print("  PART 2 — Plan A: Adaptive TTL  (threshold=2)")
    print("═" * 70)

    policies = ["infinite_cache", "lru", "ttl_lru", "two_queue_ttl"]
    total = len(capacities) * len(policies)
    run_num = 0
    all_results: list[dict] = []

    for cap in sorted(capacities):
        attl = adaptive_ttls[cap]
        ext_ttl = attl * (255 / 46)
        label = f"ttl={attl:.0f}s,thresh=2"

        configs = [
            SimConfig(cache_capacity=cap, block_size=block_size, policy="infinite_cache"),
            SimConfig(cache_capacity=cap, block_size=block_size, policy="lru"),
            SimConfig(cache_capacity=cap, block_size=block_size, policy="ttl_lru",
                      ttl_lru=TTLLRUConfig(ttl=attl)),
            SimConfig(cache_capacity=cap, block_size=block_size, policy="two_queue_ttl",
                      two_queue_ttl=TwoQueueTTLConfig(
                          base_ttl=attl,
                          extended_ttl=ext_ttl,
                          promotion_threshold=2,
                      )),
        ]
        snaps = []
        for cfg in configs:
            run_num += 1
            snap = run_one(cfg, trace, future_index, label, run_num, total)
            snaps.append(snap)
        compute_gap_closed_ratios(snaps)
        for s in snaps:
            row = {"part": "plan_a", "capacity": cap, "label": label}
            row.update(s.to_dict())
            all_results.append(row)

    _print_hit_table("Plan A (adaptive TTL)", all_results,
                     ["infinite_cache", "lru", "ttl_lru", "two_queue_ttl"])
    _print_gap_table("Plan A (adaptive TTL)", all_results,
                     ["infinite_cache", "lru", "ttl_lru", "two_queue_ttl"])
    _print_pollution_table("Plan A (adaptive TTL)", all_results)
    return all_results


# ---------------------------------------------------------------------------
# Part 3 — Plan B1 + A: threshold sweep at probe capacities
# ---------------------------------------------------------------------------

def part3_threshold_sweep(
    trace, future_index, probe_capacities, block_size, adaptive_ttls, thresholds
) -> list[dict]:
    """Sweep promotion_threshold with adaptive TTL; lru included as baseline per capacity."""
    print("\n" + "═" * 70)
    print(f"  PART 3 — Plan B1+A: threshold sweep {thresholds}  "
          f"(probe caps: {probe_capacities})")
    print("═" * 70)

    # Policies: infinite_cache + lru (references) + two_queue_ttl per threshold
    total = len(probe_capacities) * (2 + len(thresholds))
    run_num = 0
    all_results: list[dict] = []

    for cap in sorted(probe_capacities):
        attl = adaptive_ttls[cap]
        ext_ttl = attl * (255 / 46)

        # LRU reference for this capacity
        run_num += 1
        lru_cfg = SimConfig(cache_capacity=cap, block_size=block_size, policy="lru")
        lru_snap = run_one(lru_cfg, trace, future_index, "reference", run_num, total)
        inf_snap = None  # gap needs infinite_cache

        # Also need infinite_cache to compute gap — reuse from prior parts if possible,
        # but run it fresh here to keep this part self-contained.
        run_num += 1
        inf_cfg = SimConfig(cache_capacity=cap, block_size=block_size, policy="infinite_cache")
        inf_snap = run_one(inf_cfg, trace, future_index, "reference", run_num, total)

        cap_snaps_per_thresh: list[tuple[int, MetricsSnapshot]] = []

        for thresh in thresholds:
            run_num += 1
            label = f"ttl={attl:.0f}s,thresh={thresh}"
            cfg = SimConfig(
                cache_capacity=cap, block_size=block_size, policy="two_queue_ttl",
                two_queue_ttl=TwoQueueTTLConfig(
                    base_ttl=attl,
                    extended_ttl=ext_ttl,
                    promotion_threshold=thresh,
                ),
            )
            snap = run_one(cfg, trace, future_index, label, run_num, total)
            cap_snaps_per_thresh.append((thresh, snap))

        # Compute gap ratios (need infinite + lru in the list)
        all_snaps = [inf_snap, lru_snap] + [s for _, s in cap_snaps_per_thresh]
        compute_gap_closed_ratios(all_snaps)

        # Reference rows (lru, infinite_cache)
        for s in [inf_snap, lru_snap]:
            row = {"part": "plan_b1", "capacity": cap, "label": s.policy_name,
                   "promotion_threshold": None}
            row.update(s.to_dict())
            all_results.append(row)
        # Two-queue-ttl rows — tag each with its threshold
        for thresh, s in cap_snaps_per_thresh:
            row = {"part": "plan_b1", "capacity": cap,
                   "label": f"ttl={attl:.0f}s,thresh={thresh}",
                   "promotion_threshold": thresh}
            row.update(s.to_dict())
            all_results.append(row)

    # Print per-capacity summary
    print("\n  ── Threshold sweep results ──")
    for cap in sorted(probe_capacities):
        attl = adaptive_ttls[cap]
        cap_rows = [r for r in all_results
                    if r["capacity"] == cap and r["part"] == "plan_b1"
                    and r["policy"] == "two_queue_ttl"]
        lru_rows  = [r for r in all_results
                     if r["capacity"] == cap and r["part"] == "plan_b1"
                     and r["policy"] == "lru"]
        lru_hr = lru_rows[0]["prefix_block_hit_rate"] if lru_rows else float("nan")
        print(f"\n  cap={cap:,}  adaptive_ttl={attl:.0f}s  lru_baseline={lru_hr:.4f}")
        print(f"  {'thresh':>8}  {'hit_rate':>10}  {'gap_closed':>12}  {'pollution':>10}")
        for r in cap_rows:
            thresh_val = r.get("promotion_threshold", "?")
            gap = r.get("gap_closed_ratio")
            gap_str = f"{gap:>12.4f}" if gap is not None else f"{'—':>12}"
            poll = r["protected_pollution_rate"]
            print(
                f"  {thresh_val!s:>8}  {r['prefix_block_hit_rate']:>10.4f}  "
                f"{gap_str}  {poll:>10.4f}"
            )
    return all_results


# ---------------------------------------------------------------------------
# Part 4 — Best A+B1 full sweep
# ---------------------------------------------------------------------------

def part4_best_combination(
    trace, future_index, capacities, block_size, adaptive_ttls, best_threshold,
    base_ttl,
) -> list[dict]:
    """Full sweep with the best threshold from Part 3 + adaptive TTL."""
    print("\n" + "═" * 70)
    print(f"  PART 4 — Best A+B1: adaptive TTL + threshold={best_threshold}")
    print("═" * 70)

    policies = ["infinite_cache", "lru", "ttl_lru", "two_queue_ttl"]
    total = len(capacities) * len(policies)
    run_num = 0
    all_results: list[dict] = []

    for cap in sorted(capacities):
        attl = adaptive_ttls[cap]
        ext_ttl = attl * (255 / 46)
        label = f"ttl={attl:.0f}s,thresh={best_threshold}"

        configs = [
            SimConfig(cache_capacity=cap, block_size=block_size, policy="infinite_cache"),
            SimConfig(cache_capacity=cap, block_size=block_size, policy="lru"),
            SimConfig(cache_capacity=cap, block_size=block_size, policy="ttl_lru",
                      ttl_lru=TTLLRUConfig(ttl=attl)),
            SimConfig(cache_capacity=cap, block_size=block_size, policy="two_queue_ttl",
                      two_queue_ttl=TwoQueueTTLConfig(
                          base_ttl=attl,
                          extended_ttl=ext_ttl,
                          promotion_threshold=best_threshold,
                      )),
        ]
        snaps = []
        for cfg in configs:
            run_num += 1
            snap = run_one(cfg, trace, future_index, label, run_num, total)
            snaps.append(snap)
        compute_gap_closed_ratios(snaps)
        for s in snaps:
            row = {"part": "best_ab", "capacity": cap, "label": label}
            row.update(s.to_dict())
            all_results.append(row)

    _print_hit_table(f"Best A+B1 (adaptive TTL + thresh={best_threshold})",
                     all_results, ["infinite_cache", "lru", "ttl_lru", "two_queue_ttl"])
    _print_gap_table(f"Best A+B1 (adaptive TTL + thresh={best_threshold})",
                     all_results, ["infinite_cache", "lru", "ttl_lru", "two_queue_ttl"])
    _print_pollution_table(f"Best A+B1 (adaptive TTL + thresh={best_threshold})", all_results)
    return all_results


# ---------------------------------------------------------------------------
# Comparison: baseline vs best A+B1
# ---------------------------------------------------------------------------

def print_comparison(baseline_results, best_results, capacities) -> None:
    """Side-by-side delta table: best A+B1 vs baseline for two_queue_ttl."""
    print("\n" + "═" * 75)
    print("  COMPARISON: Best A+B1 vs Baseline  (two_queue_ttl only)")
    print("═" * 75)
    print(f"  {'capacity':>10}  {'baseline_hr':>12}  {'best_ab_hr':>12}  "
          f"{'Δhit_rate':>10}  {'baseline_gap':>13}  {'best_ab_gap':>12}")
    print(f"  {'─'*10}  {'─'*12}  {'─'*12}  {'─'*10}  {'─'*13}  {'─'*12}")

    for cap in sorted(capacities):
        b_row = next((r for r in baseline_results
                      if r["capacity"] == cap and r["policy"] == "two_queue_ttl"), None)
        a_row = next((r for r in best_results
                      if r["capacity"] == cap and r["policy"] == "two_queue_ttl"), None)
        if b_row is None or a_row is None:
            continue
        b_hr = b_row["prefix_block_hit_rate"]
        a_hr = a_row["prefix_block_hit_rate"]
        b_gap = b_row.get("gap_closed_ratio") or float("nan")
        a_gap = a_row.get("gap_closed_ratio") or float("nan")
        delta = a_hr - b_hr
        delta_str = f"{delta:+.4f}"
        print(f"  {cap:>10,}  {b_hr:>12.4f}  {a_hr:>12.4f}  "
              f"{delta_str:>10}  {b_gap:>13.4f}  {a_gap:>12.4f}")
    print("═" * 75 + "\n")


# ---------------------------------------------------------------------------
# Print helpers
# ---------------------------------------------------------------------------

def _print_hit_table(title: str, results: list[dict], policies: list[str]) -> None:
    col = 16
    cap_set = sorted({r["capacity"] for r in results})
    header = f"  prefix_block_hit_rate  [{title}]"
    width = 12 + col * len(policies)
    print(f"\n{'═'*width}")
    print(header)
    print(f"  {'capacity':>10}" + "".join(f"{p:>{col}}" for p in policies))
    print(f"  {'─'*10}" + "─" * (col * len(policies)))
    for cap in cap_set:
        row_data = {r["policy"]: r["prefix_block_hit_rate"]
                    for r in results if r["capacity"] == cap}
        print(f"  {cap:>10,}" + "".join(
            f"{row_data.get(p, float('nan')):>{col}.4f}" for p in policies))
    print(f"{'═'*width}\n")


def _print_gap_table(title: str, results: list[dict], policies: list[str]) -> None:
    if "infinite_cache" not in {r["policy"] for r in results}:
        return
    col = 16
    cap_set = sorted({r["capacity"] for r in results})
    header = f"  gap_closed_ratio  [{title}]"
    width = 12 + col * len(policies)
    print(f"{'═'*width}")
    print(header)
    print(f"  {'capacity':>10}" + "".join(f"{p:>{col}}" for p in policies))
    print(f"  {'─'*10}" + "─" * (col * len(policies)))
    for cap in cap_set:
        row_data = {r["policy"]: r.get("gap_closed_ratio")
                    for r in results if r["capacity"] == cap}
        cells = []
        for p in policies:
            v = row_data.get(p)
            cells.append(f"{v:{col}.4f}" if v is not None else f"{'—':>{col}}")
        print(f"  {cap:>10,}" + "".join(cells))
    print(f"{'═'*width}\n")


def _print_pollution_table(title: str, results: list[dict]) -> None:
    tq_rows = [r for r in results if r["policy"] == "two_queue_ttl"]
    if not tq_rows:
        return
    col = 16
    cap_set = sorted({r["capacity"] for r in tq_rows})
    print(f"{'═'*(12+col)}")
    print(f"  protected_pollution_rate  [{title}]  (two_queue_ttl)")
    print(f"  {'capacity':>10}  {'pollution_rate':>{col-2}}")
    print(f"  {'─'*10}  {'─'*(col-2)}")
    for cap in cap_set:
        row = next((r for r in tq_rows if r["capacity"] == cap), None)
        if row:
            print(f"  {cap:>10,}  {row['protected_pollution_rate']:>{col-2}.4f}")
    print(f"{'═'*(12+col)}\n")


# ---------------------------------------------------------------------------
# Save results
# ---------------------------------------------------------------------------

def save_results(all_parts: list[dict], output_dir: str) -> None:
    os.makedirs(output_dir, exist_ok=True)
    path_csv  = os.path.join(output_dir, "plan_ab_results.csv")
    path_json = os.path.join(output_dir, "plan_ab_results.json")
    if all_parts:
        # Collect union of all field names to handle rows with different keys.
        fieldnames: list[str] = []
        seen: set[str] = set()
        for row in all_parts:
            for k in row:
                if k not in seen:
                    fieldnames.append(k)
                    seen.add(k)
        with open(path_csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore",
                                    restval="")
            writer.writeheader()
            writer.writerows(all_parts)
    with open(path_json, "w") as f:
        json.dump(all_parts, f, indent=2)
    print(f"Results saved: {path_csv}")
    print(f"              {path_json}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _pick_best_threshold(part3_results: list[dict], probe_capacities: list[int],
                          thresholds: list[int]) -> int:
    """Pick threshold with the best average gap_closed_ratio across probe capacities."""
    scores: dict[int, list[float]] = {t: [] for t in thresholds}
    for cap in probe_capacities:
        for r in part3_results:
            if r["capacity"] != cap or r["policy"] != "two_queue_ttl":
                continue
            t = r.get("promotion_threshold")
            if t in scores:
                gap = r.get("gap_closed_ratio")
                if gap is not None:
                    scores[t].append(gap)
    avg_scores = {t: (sum(v) / len(v) if v else float("-inf")) for t, v in scores.items()}
    best = max(avg_scores, key=avg_scores.__getitem__)
    print("\n  Threshold selection (avg gap_closed_ratio across probe capacities):")
    for t in sorted(thresholds):
        marker = " ← SELECTED" if t == best else ""
        print(f"    threshold={t}  avg_gap={avg_scores[t]:.4f}{marker}")
    return best


def main() -> None:
    args = parse_args()

    # ── Load trace ──────────────────────────────────────────────────────────
    print(f"\nLoading trace: {args.trace}")
    if args.raw:
        trace = load_raw_trace(args.trace, block_size=args.block_size)
    else:
        trace = load_trace(args.trace)
    print(f"  {len(trace):,} records loaded")

    precompute_prefix_keys(trace)

    # ── Build future index once ─────────────────────────────────────────────
    print("\n  Building future access index …", flush=True)
    t0 = time.time()
    future_index = build_future_index(trace)
    print(f"  Future index ready ({time.time()-t0:.1f}s, "
          f"{len(future_index):,} unique block keys)", flush=True)

    # ── Compute adaptive TTL values ─────────────────────────────────────────
    all_caps = sorted(set(args.capacities) | set(args.probe_capacities))
    adaptive_ttls = compute_adaptive_ttls(
        future_index, trace, all_caps, args.base_ttl, args.alpha
    )

    all_results: list[dict] = []

    # ── Part 1: Baseline ────────────────────────────────────────────────────
    if not args.skip_baseline:
        r1 = part1_baseline(trace, future_index, args.capacities,
                             args.block_size, args.base_ttl, args)
        all_results.extend(r1)
    else:
        print("\n  [Part 1 skipped — --skip-baseline]")
        r1 = []

    # ── Part 2: Plan A (adaptive TTL, threshold=2) ──────────────────────────
    r2 = part2_adaptive_ttl(trace, future_index, args.capacities,
                             args.block_size, adaptive_ttls, args.base_ttl)
    all_results.extend(r2)

    # ── Part 3: Plan B1+A (threshold sweep at probe capacities) ─────────────
    r3 = part3_threshold_sweep(trace, future_index, args.probe_capacities,
                                args.block_size, adaptive_ttls, args.thresholds)
    all_results.extend(r3)

    # ── Select best threshold ────────────────────────────────────────────────
    best_threshold = _pick_best_threshold(r3, args.probe_capacities, args.thresholds)

    # ── Part 4: Best A+B1 full sweep ────────────────────────────────────────
    r4 = part4_best_combination(trace, future_index, args.capacities,
                                 args.block_size, adaptive_ttls,
                                 best_threshold, args.base_ttl)
    all_results.extend(r4)

    # ── Final comparison ─────────────────────────────────────────────────────
    if r1:
        print_comparison(r1, r4, args.capacities)

    # ── Save ─────────────────────────────────────────────────────────────────
    save_results(all_results, args.output_dir)


if __name__ == "__main__":
    main()
