#!/usr/bin/env python3
"""Step 1.2 supplement — branch_threshold sweep + per-user chain length plot.

Builds the global trie + per-user tries ONCE, evaluates LCP at every threshold
in [0.00, 0.05, ..., 1.00], and plots chain length vs threshold for each user.

Heavy users (request share >= --heavy-pct-threshold, default 20%) are drawn with
triangle markers + thick lines so the dominant chains stand out.

Workflow
--------
  1. Run this sweep to pick a good `branch_threshold` visually
  2. Then run `per_user_chain_analyzer.py` at the chosen threshold to get the
     full per_user_chains.json analysis

Outputs
-------
  --output FOO.png  →  saves FOO.png and FOO.csv (raw sweep data)

Reusable across models. Same input contract as Step 1.1/1.2.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import defaultdict
from pathlib import Path

# Reuse Step 1.1/1.2 primitives
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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--raw-csv", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path,
                   help="Output PNG path. CSV with raw sweep data is saved "
                        "alongside (same basename, .csv extension)")
    p.add_argument("--coverage-threshold", type=float, default=0.05,
                   help="Held fixed during the sweep (default 0.05)")
    p.add_argument("--block-size", type=int, default=128)
    p.add_argument("--threshold-step", type=float, default=0.05,
                   help="branch_threshold step size (default 0.05 → 21 points)")
    p.add_argument("--heavy-pct", type=float, default=0.20,
                   help="Users with request share >= this get triangle markers "
                        "(default 0.20)")
    p.add_argument("--default-threshold", type=float, default=0.25,
                   help="Vertical reference line on the plot (default 0.25, "
                        "the recommended production default after 05-12 revision; "
                        "pass 0.45 to compare against the historical 04-30 default)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    csv_files = discover_csv_files(args.raw_csv)
    if not csv_files:
        print(f"No CSV files found at {args.raw_csv}", file=sys.stderr)
        sys.exit(1)
    print(f"Input: {len(csv_files)} CSV file(s) under {args.raw_csv}", flush=True)

    # ---- Phase 1: build global trie + per-user tries (single pass) ----
    t0 = time.time()
    global_root = TrieNode()
    user_roots: dict[str, TrieNode] = defaultdict(TrieNode)
    user_request_count: dict[str, int] = defaultdict(int)
    n_total = 0

    for request_id, user_id, raw_prompt, _ts in iter_raw_records(csv_files):
        n_total += 1
        user_request_count[user_id] += 1
        if not raw_prompt:
            global_root.count += 1
            user_roots[user_id].count += 1
            continue
        blocks = split_blocks(raw_prompt, args.block_size)
        keys = compute_prefix_path_keys(blocks)
        trie_insert(global_root, keys, request_id)
        trie_insert(user_roots[user_id], keys, request_id)
        if n_total % 1000 == 0:
            print(f"  {n_total:>7,} requests", flush=True)

    print(
        f"Built global + {len(user_roots)} per-user tries "
        f"({n_total:,} requests) in {time.time()-t0:.1f}s",
        flush=True,
    )

    # ---- Phase 2: sweep thresholds ----
    n_steps = int(round(1.0 / args.threshold_step)) + 1
    thresholds = [round(i * args.threshold_step, 4) for i in range(n_steps)]

    user_ids = list(user_roots.keys())
    user_chain_lengths: dict[str, list[int]] = {uid: [] for uid in user_ids}
    global_chain_lengths: list[int] = []

    t1 = time.time()
    for thresh in thresholds:
        chain, _ = find_lcp(global_root, thresh, args.coverage_threshold)
        global_chain_lengths.append(len(chain))
        for uid in user_ids:
            chain, _ = find_lcp(user_roots[uid], thresh, args.coverage_threshold)
            user_chain_lengths[uid].append(len(chain))
    print(
        f"Swept {len(thresholds)} thresholds × {len(user_ids) + 1} entities "
        f"in {time.time()-t1:.2f}s",
        flush=True,
    )

    # ---- Phase 3: write CSV ----
    args.output.parent.mkdir(parents=True, exist_ok=True)
    csv_out = args.output.with_suffix(".csv")
    sorted_users = sorted(user_ids, key=lambda u: -user_request_count[u])
    with open(csv_out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["threshold", "global"] + sorted_users)
        for i, thresh in enumerate(thresholds):
            row = [thresh, global_chain_lengths[i]]
            for uid in sorted_users:
                row.append(user_chain_lengths[uid][i])
            w.writerow(row)
    print(f"data → {csv_out}", flush=True)

    # ---- Phase 4: plot ----
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skipping plot. CSV still written.",
              file=sys.stderr)
        return

    fig, ax = plt.subplots(figsize=(13, 7.5))

    # Distinct colors via tab20 (good for ≤20 lines)
    cmap = plt.get_cmap("tab20")
    n_users = len(sorted_users)

    heavy_count = 0
    for i, uid in enumerate(sorted_users):
        share = user_request_count[uid] / n_total if n_total else 0.0
        is_heavy = share >= args.heavy_pct
        if is_heavy:
            heavy_count += 1
        color = cmap(i % 20)

        # Truncate long user_id for legend
        uid_disp = uid if len(uid) <= 36 else uid[:33] + "..."
        label = (f"{uid_disp}  ({user_request_count[uid]:,} reqs, "
                 f"{share*100:.1f}%)")

        if is_heavy:
            ax.plot(thresholds, user_chain_lengths[uid],
                    color=color, marker="^", markersize=10,
                    linewidth=2.2, label=label, zorder=5)
        else:
            ax.plot(thresholds, user_chain_lengths[uid],
                    color=color, marker="o", markersize=4,
                    linewidth=1.0, alpha=0.7, label=label)

    # Global line — black thick, star markers
    ax.plot(thresholds, global_chain_lengths,
            color="black", marker="*", markersize=12,
            linewidth=2.8, label=f"GLOBAL ({n_total:,} reqs)",
            zorder=10)

    # Reference vertical line at recommended default
    ax.axvline(args.default_threshold, color="red", linestyle="--",
               linewidth=1.2, alpha=0.6,
               label=f"recommended default = {args.default_threshold}",
               zorder=1)

    # Cosmetics
    ax.set_xlabel("branch_threshold", fontsize=11)
    ax.set_ylabel("chain_length (blocks)", fontsize=11)
    title = (
        f"Chain length vs branch_threshold sweep — {args.raw_csv}\n"
        f"coverage_threshold={args.coverage_threshold}, "
        f"block_size={args.block_size}, "
        f"{n_total:,} requests, {n_users} users "
        f"({heavy_count} heavy ≥ {args.heavy_pct*100:.0f}%)"
    )
    ax.set_title(title, fontsize=10)
    ax.set_xlim(-0.02, 1.02)
    ax.set_xticks([round(i * 0.1, 1) for i in range(11)])
    ax.grid(True, alpha=0.3)
    ax.legend(
        bbox_to_anchor=(1.02, 1.0),
        loc="upper left",
        fontsize=7.5,
        title="user_id (req count, share%) — heavy=▲",
        title_fontsize=8,
    )

    plt.tight_layout()
    plt.savefig(args.output, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"plot → {args.output}", flush=True)

    # ---- Console summary ----
    print("\n=== Sweep summary ===", flush=True)
    print(f"  total requests : {n_total:,}", flush=True)
    print(f"  total users    : {n_users}  (heavy ≥ {args.heavy_pct*100:.0f}%: "
          f"{heavy_count})", flush=True)
    print(f"  thresholds     : {len(thresholds)} points "
          f"({thresholds[0]} → {thresholds[-1]} step {args.threshold_step})",
          flush=True)
    # Show global length at a few key thresholds
    print("\n  Global chain length at key thresholds:", flush=True)
    for t in [0.0, 0.30, 0.45, 0.50, 0.80, 0.95, 1.0]:
        if t in thresholds:
            idx = thresholds.index(t)
            print(f"    threshold={t:.2f}  →  chain_length={global_chain_lengths[idx]}",
                  flush=True)


if __name__ == "__main__":
    main()
