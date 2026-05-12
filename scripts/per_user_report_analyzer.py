#!/usr/bin/env python3
"""Step 1.5 — Per-user research report orchestrator.

Picks the Top-K eligible users from a raw CSV (request_count >= 1% of
total by default), then for each selected user produces four artifacts:

  * ideal KV cache hit rate (vLLM block-level, user-internal — D1)
  * request arrival time series + inter-arrival quantiles (§4.2)
  * new unique block/s time series + cumulative WS (§4.3)
  * multi-chain forest with decoded chain content (§4.4 + §5)

Output layout per design §2:
  outputs/<dataset>/per_user_reports/
    user_summary.json
    user_summary.csv
    <user_id>/user_report.json
    <user_id>/chain_forest.json

This script DOES NOT render HTML — that's `render_user_report_html.py`.

Reusable across models; tokenizer-free; offline-safe.

Usage
-----
  python scripts/per_user_report_analyzer.py \\
      --raw-csv             data/<dataset>/raw \\
      --output-dir          outputs/<dataset>/per_user_reports \\
      --top-k-users         3 \\
      --min-request-pct     0.01 \\
      --mc-branch-threshold     0.05 \\
      --mc-coverage-threshold   0.05 \\
      --mc-min-chain-length     10 \\
      --mc-min-chain-coverage   0.01 \\
      --mc-max-chains           50
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from verify_chain_path_closure import (  # noqa: E402
    TrieNode,
    compute_prefix_path_keys,
    discover_csv_files,
    iter_raw_records,
    split_blocks,
    trie_insert,
)
from multi_chain_finder import (  # noqa: E402
    DEFAULT_MC_BRANCH_THRESHOLD,
    DEFAULT_MC_COVERAGE_THRESHOLD,
    DEFAULT_MIN_CHAIN_LENGTH,
    DEFAULT_MIN_CHAIN_COVERAGE,
    DEFAULT_MAX_CHAINS,
    find_chain_forest,
)

csv.field_size_limit(sys.maxsize)

DEFAULT_TOP_K = 3
DEFAULT_MIN_REQUEST_PCT = 0.01


# ---------------------------------------------------------------------------
# Pass 1: per-user request counts (no raw_prompt held in memory)
# ---------------------------------------------------------------------------

def collect_user_counts(csv_files: list[Path]) -> tuple[dict[str, int], int]:
    counts: dict[str, int] = defaultdict(int)
    total = 0
    for _rid, user_id, _prompt, _ts in iter_raw_records(csv_files):
        counts[user_id] += 1
        total += 1
    return dict(counts), total


def select_users(
    counts: dict[str, int], total: int, top_k: int, min_pct: float,
) -> tuple[list[str], list[dict]]:
    """Return (selected_user_ids, excluded_list_with_reasons)."""
    sorted_users = sorted(counts.items(), key=lambda kv: -kv[1])
    selected: list[str] = []
    excluded: list[dict] = []
    for i, (uid, n) in enumerate(sorted_users):
        pct = n / total if total else 0.0
        if i < top_k and pct >= min_pct:
            selected.append(uid)
        else:
            reason = "below_min_pct" if pct < min_pct else "outside_top_k"
            excluded.append({
                "user_id": uid, "request_count": n,
                "request_pct": round(pct * 100, 3), "reason": reason,
            })
    return selected, excluded


# ---------------------------------------------------------------------------
# Pass 2: collect raw_prompt for selected users only
# ---------------------------------------------------------------------------

def collect_records_for_selected(
    csv_files: list[Path], selected: set[str],
) -> dict[str, list[tuple[str, int, str]]]:
    """Returns user_id -> list of (request_id, timestamp_seconds, raw_prompt).

    timestamp from iter_raw_records is a string; floor to integer seconds
    per D5 (timestamp精度是整数秒).
    """
    out: dict[str, list[tuple[str, int, str]]] = {uid: [] for uid in selected}
    for rid, uid, prompt, ts in iter_raw_records(csv_files):
        if uid in selected:
            try:
                ts_int = int(float(ts))
            except (ValueError, TypeError):
                ts_int = 0
            out[uid].append((rid, ts_int, prompt or ""))
    return out


# ---------------------------------------------------------------------------
# Per-user analysis
# ---------------------------------------------------------------------------

def percentile_int(sorted_values: list[int], pct: float) -> int:
    """Linear-interpolation percentile; pct in [0, 100]. Empty -> 0."""
    if not sorted_values:
        return 0
    if pct <= 0:
        return sorted_values[0]
    if pct >= 100:
        return sorted_values[-1]
    k = (len(sorted_values) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    return int(round(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)))


def lcp_histogram(lcps: list[int]) -> tuple[list[dict], dict]:
    """Equal-width histogram with ~30 buckets; returns (buckets, quantiles)."""
    if not lcps:
        return [], {"p50": 0, "p95": 0, "max": 0}
    sorted_lcps = sorted(lcps)
    max_lcp = sorted_lcps[-1]
    bucket_size = max(1, max_lcp // 30)
    buckets: dict[int, int] = defaultdict(int)
    for v in lcps:
        buckets[v // bucket_size] += 1
    histogram = [
        {
            "lcp_low":  b * bucket_size,
            "lcp_high": (b + 1) * bucket_size - 1,
            "count":    n,
        }
        for b, n in sorted(buckets.items())
    ]
    quantiles = {
        "p50": percentile_int(sorted_lcps, 50),
        "p95": percentile_int(sorted_lcps, 95),
        "max": max_lcp,
        "bucket_size": bucket_size,
    }
    return histogram, quantiles


def walk_chain_leaf(root: TrieNode, hex_keys: list[str]) -> TrieNode:
    """Walk trie along chain keys to find the leaf node. Returns None if path missing."""
    node = root
    for hk in hex_keys:
        k = bytes.fromhex(hk)
        node = node.children.get(k)
        if node is None:
            return None
    return node


def decode_chain_content(
    chain: dict, trie_root: TrieNode,
    prompts_by_rid: dict[str, str], block_size: int,
) -> tuple[str, list[dict]]:
    """Find a sample request for the chain leaf and decode its first N blocks."""
    leaf = walk_chain_leaf(trie_root, chain["keys"])
    sample_rid = leaf.sample_request_id if leaf else None
    prompt = prompts_by_rid.get(sample_rid) if sample_rid else None

    decoded_blocks: list[bytes] = []
    if prompt:
        decoded_blocks = split_blocks(prompt, block_size)

    content = []
    for i, (hk, cnt) in enumerate(zip(chain["keys"], chain["counts"])):
        text = None
        if i < len(decoded_blocks):
            text = decoded_blocks[i].decode("utf-8", errors="replace")
        content.append({
            "position":         i,
            "prefix_path_key":  hk,
            "count":            cnt,
            "decoded_text":     text,
        })
    return sample_rid, content


def analyze_user(
    user_id: str,
    records: list[tuple[str, int, str]],
    args: argparse.Namespace,
) -> tuple[dict, dict, TrieNode]:
    """One pass over the user's records, then chain forest + decode.

    Returns (user_report, chain_forest, trie_root).
    """
    t0 = time.time()

    # Sort by timestamp (stable on rid for determinism)
    records = sorted(records, key=lambda r: (r[1], r[0]))
    n_total = len(records)

    # Single pass: trie build + LCP/hit + new_block/s + req/min + gaps
    root = TrieNode()
    seen_keys: set[bytes] = set()
    hit_blocks = 0
    total_blocks = 0
    empty_prompts = 0
    new_per_sec: dict[int, int] = defaultdict(int)
    req_per_min: dict[int, int] = defaultdict(int)
    inter_arrival: list[int] = []
    per_request_lcp: list[int] = []
    prev_ts: int | None = None
    prompts_by_rid: dict[str, str] = {}
    earliest_ts: int | None = None
    latest_ts: int | None = None

    for rid, ts, prompt in records:
        prompts_by_rid[rid] = prompt
        req_per_min[ts // 60] += 1
        if earliest_ts is None:
            earliest_ts = ts
        latest_ts = ts
        if prev_ts is not None:
            inter_arrival.append(ts - prev_ts)
        prev_ts = ts

        if not prompt:
            empty_prompts += 1
            root.count += 1
            per_request_lcp.append(0)
            continue

        blocks = split_blocks(prompt, args.block_size)
        keys = compute_prefix_path_keys(blocks)
        total_blocks += len(keys)

        # LCP: longest prefix-run of seen keys.
        # By prefix_path_key hash-chain property, "key_i seen" implies
        # "key_0..key_{i-1} all seen", so a single sequential scan is correct.
        lcp = 0
        for k in keys:
            if k in seen_keys:
                lcp += 1
            else:
                break
        hit_blocks += lcp
        per_request_lcp.append(lcp)

        # Mark new blocks
        for k in keys:
            if k not in seen_keys:
                seen_keys.add(k)
                new_per_sec[ts] += 1

        trie_insert(root, keys, rid)

    # Inter-arrival quantiles
    sorted_gaps = sorted(inter_arrival)
    gap_q = {
        "p50": percentile_int(sorted_gaps, 50),
        "p75": percentile_int(sorted_gaps, 75),
        "p80": percentile_int(sorted_gaps, 80),
        "p95": percentile_int(sorted_gaps, 95),
        "max": sorted_gaps[-1] if sorted_gaps else 0,
    }

    # New-block/s quantiles (over seconds that had at least one new block)
    new_per_sec_vals = sorted(new_per_sec.values())
    new_q = {
        "p50": percentile_int(new_per_sec_vals, 50),
        "p95": percentile_int(new_per_sec_vals, 95),
        "max": new_per_sec_vals[-1] if new_per_sec_vals else 0,
    }

    # Time series (sorted by key)
    req_per_min_series = [
        {"minute": m, "count": req_per_min[m]} for m in sorted(req_per_min)
    ]
    new_per_sec_series = [
        {"second": s, "count": new_per_sec[s]} for s in sorted(new_per_sec)
    ]
    cumulative_unique: list[dict] = []
    running = 0
    for entry in new_per_sec_series:
        running += entry["count"]
        cumulative_unique.append({"second": entry["second"], "total": running})

    # LCP histogram
    histogram, lcp_quantiles = lcp_histogram(per_request_lcp)

    # Chain forest
    t1 = time.time()
    forest = find_chain_forest(
        root,
        mc_branch_thr=args.mc_branch_threshold,
        mc_cov_thr=args.mc_coverage_threshold,
        min_chain_length=args.mc_min_chain_length,
        min_chain_coverage=args.mc_min_chain_coverage,
        max_chains=args.mc_max_chains,
    )
    forest_seconds = time.time() - t1

    # Decode each chain's content
    for chain in forest["chains"]:
        sample_rid, decoded = decode_chain_content(
            chain, root, prompts_by_rid, args.block_size,
        )
        chain["sample_request_id"] = sample_rid
        chain["decoded_content"]   = decoded

    forest["user_id"] = user_id
    forest["params"]["block_size"] = args.block_size
    forest["stats"]["forest_seconds"] = round(forest_seconds, 3)

    ideal_hit_rate = hit_blocks / total_blocks if total_blocks else 0.0

    # Top-chain summary for user_summary
    chain_forest_summary = {
        "total_chains":    len(forest["chains"]),
        "dominant_chain_coverage_pct": (
            forest["chains"][0]["coverage_pct"] if forest["chains"] else 0.0
        ),
        "dominant_chain_length": (
            forest["chains"][0]["chain_length"] if forest["chains"] else 0
        ),
    }

    user_report = {
        "user_id": user_id,
        "params": {
            "block_size":               args.block_size,
            "mc_branch_threshold":      args.mc_branch_threshold,
            "mc_coverage_threshold":    args.mc_coverage_threshold,
            "mc_min_chain_length":      args.mc_min_chain_length,
            "mc_min_chain_coverage":    args.mc_min_chain_coverage,
            "mc_max_chains":            args.mc_max_chains,
        },
        "stats": {
            "total_requests":  n_total,
            "total_blocks":    total_blocks,
            "empty_prompts":   empty_prompts,
            "unique_blocks":   len(seen_keys),
            "hit_blocks":      hit_blocks,
            "ideal_hit_rate":  round(ideal_hit_rate, 6),
            "earliest_timestamp": earliest_ts,
            "latest_timestamp":   latest_ts,
            "analyze_seconds":    round(time.time() - t0, 3),
        },
        "inter_arrival_gaps_seconds":   gap_q,
        "new_unique_blocks_per_sec_q":  new_q,
        "lcp_distribution": {
            "quantiles":  lcp_quantiles,
            "histogram":  histogram,
        },
        "time_series": {
            "requests_per_minute":          req_per_min_series,
            "new_unique_blocks_per_second": new_per_sec_series,
            "cumulative_unique_blocks":     cumulative_unique,
        },
        "chain_forest_summary": chain_forest_summary,
        "caveats": [
            "timestamp is integer seconds; sub-second arrival order is lost. "
            "Inter-arrival p50/p75 and same-second LCP/new-block stats may be biased "
            "(batch-internal requests counted as 'simultaneous').",
        ],
    }

    return user_report, forest, root


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def write_summary(
    output_dir: Path,
    selected_users: list[str],
    excluded: list[dict],
    user_reports: dict[str, dict],
    total_requests: int,
    total_user_count: int,
) -> None:
    rows = []
    for uid in selected_users:
        r = user_reports[uid]
        s = r["stats"]
        cs = r["chain_forest_summary"]
        rows.append({
            "user_id":                    uid,
            "request_count":              s["total_requests"],
            "request_pct":                round(s["total_requests"] / total_requests * 100, 3)
                                          if total_requests else 0.0,
            "ideal_hit_rate":             s["ideal_hit_rate"],
            "chain_forest_count":         cs["total_chains"],
            "dominant_chain_cov_pct":     cs["dominant_chain_coverage_pct"],
            "dominant_chain_length":      cs["dominant_chain_length"],
            "p50_gap":                    r["inter_arrival_gaps_seconds"]["p50"],
            "p95_gap":                    r["inter_arrival_gaps_seconds"]["p95"],
            "new_block_per_sec_p95":      r["new_unique_blocks_per_sec_q"]["p95"],
        })

    summary_json = {
        "total_requests":      total_requests,
        "total_users":         total_user_count,
        "selected_users":      rows,
        "excluded_users":      excluded,
    }
    summary_path = output_dir / "user_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary_json, f, indent=2, ensure_ascii=False)

    csv_path = output_dir / "user_summary.csv"
    fieldnames = list(rows[0].keys()) if rows else [
        "user_id", "request_count", "request_pct", "ideal_hit_rate",
        "chain_forest_count", "dominant_chain_cov_pct", "dominant_chain_length",
        "p50_gap", "p95_gap", "new_block_per_sec_p95",
    ]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def safe_dirname(user_id: str) -> str:
    """Sanitize user_id for use as a directory name."""
    s = _UNSAFE_NAME.sub("_", user_id)
    return s[:128] if len(s) > 128 else s


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--raw-csv",    required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--top-k-users",     type=int,   default=DEFAULT_TOP_K)
    p.add_argument("--min-request-pct", type=float, default=DEFAULT_MIN_REQUEST_PCT)
    p.add_argument("--block-size",      type=int,   default=128)
    p.add_argument("--mc-branch-threshold",   type=float, default=DEFAULT_MC_BRANCH_THRESHOLD)
    p.add_argument("--mc-coverage-threshold", type=float, default=DEFAULT_MC_COVERAGE_THRESHOLD)
    p.add_argument("--mc-min-chain-length",   type=int,   default=DEFAULT_MIN_CHAIN_LENGTH)
    p.add_argument("--mc-min-chain-coverage", type=float, default=DEFAULT_MIN_CHAIN_COVERAGE)
    p.add_argument("--mc-max-chains",         type=int,   default=DEFAULT_MAX_CHAINS)
    return p.parse_args()


def main() -> None:
    args = parse_args()

    csv_files = discover_csv_files(args.raw_csv)
    if not csv_files:
        print(f"error: no CSV files at {args.raw_csv}", file=sys.stderr)
        sys.exit(1)
    print(f"Input: {len(csv_files)} CSV file(s) under {args.raw_csv}", flush=True)

    # Pass 1: counts
    t_pass1 = time.time()
    counts, total = collect_user_counts(csv_files)
    print(
        f"Pass 1: {total:,} requests across {len(counts)} users  "
        f"({time.time()-t_pass1:.1f}s)",
        flush=True,
    )

    selected, excluded = select_users(
        counts, total, args.top_k_users, args.min_request_pct,
    )
    print(
        f"Selected {len(selected)} user(s) (top_k={args.top_k_users}, "
        f"min_pct={args.min_request_pct*100:.1f}%):",
        flush=True,
    )
    for uid in selected:
        n = counts[uid]
        print(f"  {uid:<40} reqs={n:>7,}  pct={n/total*100:>5.2f}%", flush=True)
    if not selected:
        print("warning: no users met the criteria; nothing to analyze.", file=sys.stderr)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        write_summary(args.output_dir, [], excluded, {}, total, len(counts))
        return

    # Pass 2: collect records for selected
    t_pass2 = time.time()
    selected_set = set(selected)
    records_by_user = collect_records_for_selected(csv_files, selected_set)
    print(f"Pass 2: collected records  ({time.time()-t_pass2:.1f}s)", flush=True)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    user_reports: dict[str, dict] = {}

    for uid in selected:
        print(f"\nAnalyzing {uid} ({counts[uid]:,} requests)...", flush=True)
        report, forest, _root = analyze_user(uid, records_by_user[uid], args)

        udir = args.output_dir / safe_dirname(uid)
        udir.mkdir(parents=True, exist_ok=True)

        with open(udir / "user_report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        with open(udir / "chain_forest.json", "w", encoding="utf-8") as f:
            json.dump(forest, f, indent=2, ensure_ascii=False)

        user_reports[uid] = report
        s = report["stats"]
        cs = report["chain_forest_summary"]
        print(
            f"  ideal_hit_rate={s['ideal_hit_rate']:.4f}  "
            f"unique_blocks={s['unique_blocks']:,}  "
            f"chains={cs['total_chains']}  "
            f"dom_cov={cs['dominant_chain_coverage_pct']:.1f}%  "
            f"({s['analyze_seconds']:.1f}s)",
            flush=True,
        )

    write_summary(args.output_dir, selected, excluded, user_reports, total, len(counts))
    print(f"\n  output → {args.output_dir}/user_summary.{{json,csv}}", flush=True)


if __name__ == "__main__":
    main()
