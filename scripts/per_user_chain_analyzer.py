#!/usr/bin/env python3
"""Step 1.2 — Per-user prefix-path chain detection.

For each user_id (实为 product_id), build an independent trie and find the
LCP using the same algorithm as Step 1.1. Outputs:
  - Each user's chain (length, coverage, decoded content, branch points)
  - Comparison with the global (cross-user) chain (prefix match, identical?)
  - Aggregate statistics across users

This script reuses Step 1.1 primitives directly (trie, hashing, LCP finder,
decoder), keeping the algorithm definition in one place.

Reusable across models. Same input contract as Step 1.1 (4-column raw CSV).
Tokenizer-free; offline-safe.

Usage
-----
  python scripts/per_user_chain_analyzer.py \
      --raw-csv data/dsk8k_2h_5k/raw \
      --output  outputs/dsk8k_2h_5k/per_user_chains.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

# Reuse Step 1.1 primitives
sys.path.insert(0, str(Path(__file__).parent))
# Step 1.6: lib/ provides PromptEncoder Protocol + Byte/GLM5 implementations
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from verify_chain_path_closure import (  # noqa: E402
    TrieNode,
    _canon_fieldnames,
    discover_csv_files,
    find_lcp,
    find_sample_request_at_depth,
    iter_raw_records,
    split_blocks,
    trie_insert,
)
from lib.prompt_encoder import build_encoder_from_args  # noqa: E402
# Reuse Step 1.5 v2 metrics (spike detection + percentile)
from per_user_report_analyzer import (  # noqa: E402
    DEFAULT_SPIKE_WINDOW_MIN,
    DEFAULT_SPIKE_THRESHOLD,
    detect_traffic_spikes,
    percentile_int,
)

csv.field_size_limit(sys.maxsize)

# v2 threshold sweep config (per_user_chains_html_redesign.md §5.7)
SWEEP_THRESHOLDS = [round(i * 0.05, 2) for i in range(21)]  # 0.00 → 1.00 step 0.05


def quantile_summary(values: list[int], total_count: Optional[int] = None) -> dict:
    """Return {avg, p50, p80, p95, max} over a (possibly sparse) bucket series.

    `values` is the list of *non-zero* bucket counts (e.g., new_unique_per_sec
    dict values — only seconds with ≥ 1 new block).

    `total_count` is the number of buckets we *should* be observing (e.g.,
    trace_duration_seconds for a per-second metric). If total_count > len(values),
    the missing buckets are treated as zero — this matches the intuitive
    "瞬时压力分位数" reading: "95% of all seconds have ≤ X blocks/s".

    Without total_count, falls back to len(values) — that means quantiles are
    over *non-zero* buckets only (legacy behavior, will overstate spike-y series).
    """
    if not values and not total_count:
        return {"avg": 0.0, "p50": 0, "p80": 0, "p95": 0, "max": 0}

    sorted_vals = sorted(values)
    if total_count is not None and total_count > len(sorted_vals):
        # Pad zero buckets in front (already sorted: 0s come first)
        n_zeros = total_count - len(sorted_vals)
        denom = total_count
        sum_vals = sum(sorted_vals)  # zeros contribute 0
        # Combined sorted list: [0]*n_zeros + sorted_vals
        # For percentile: index = pct/100 × (denom-1)
        def quant_at(pct: float) -> int:
            if denom == 0:
                return 0
            idx = (denom - 1) * pct / 100.0
            lo = int(idx)
            if lo < n_zeros:
                return 0
            return sorted_vals[lo - n_zeros]
        return {
            "avg": round(sum_vals / denom, 4),
            "p50": quant_at(50),
            "p80": quant_at(80),
            "p95": quant_at(95),
            "max": sorted_vals[-1] if sorted_vals else 0,
        }

    # Fallback: legacy non-zero quantile (also used if values fully populate total_count)
    if not sorted_vals:
        return {"avg": 0.0, "p50": 0, "p80": 0, "p95": 0, "max": 0}
    avg = sum(sorted_vals) / len(sorted_vals)
    return {
        "avg": round(avg, 4),
        "p50": percentile_int(sorted_vals, 50),
        "p80": percentile_int(sorted_vals, 80),
        "p95": percentile_int(sorted_vals, 95),
        "max": sorted_vals[-1],
    }


# ---------------------------------------------------------------------------
# Batched decoding
# ---------------------------------------------------------------------------

def batch_decode(
    csv_files: list[Path],
    needed: dict[str, int],   # request_id -> max_blocks_to_decode
    block_size: int,
) -> dict[str, list[str]]:
    """Single-pass over CSV files to decode multiple chains efficiently."""
    out: dict[str, list[str]] = {}
    if not needed:
        return out
    remaining = set(needed)
    for csv_path in csv_files:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            mapping = _canon_fieldnames(reader.fieldnames or [])
            rid_col = mapping.get("request_id")
            prompt_col = mapping.get("raw_prompt")
            if not rid_col or not prompt_col:
                continue
            for row in reader:
                rid = row.get(rid_col)
                if rid in remaining:
                    n = needed[rid]
                    blocks = split_blocks(row[prompt_col], block_size)
                    out[rid] = [
                        b.decode("utf-8", errors="replace") for b in blocks[:n]
                    ]
                    remaining.discard(rid)
                    if not remaining:
                        return out
    return out


# ---------------------------------------------------------------------------
# Chain comparison
# ---------------------------------------------------------------------------

def common_prefix_length(a: list[bytes], b: list[bytes]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--raw-csv", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--branch-threshold", type=float, default=0.25,
                   help="default 0.25 (recommended after 05-12 multi-model "
                        "evidence; 0.45 was the 04-30 default but missed chains "
                        "with ratio in 0.15-0.45 band); 0.95 for strict closure")
    p.add_argument("--coverage-threshold", type=float, default=0.05)
    p.add_argument("--block-size", type=int, default=128,
                   help="bytes (byte encoder) or tokens (glm5_token / hf_token encoder) per block")
    # Step 1.6: Encoder strategy (byte = regression baseline; glm5_token/hf_token =精确化)
    p.add_argument("--encoder", type=str, default="byte",
                   choices=["byte", "glm5_token", "hf_token"],
                   help="prompt encoding strategy (default: byte; "
                        "glm5_token = back-compat alias for models/glm5_tokenizer; "
                        "hf_token = any HF tokenizer via --tokenizer-path)")
    p.add_argument("--tokenizer-path", type=str, default="models/glm5_tokenizer",
                   help="HF tokenizer dir or repo id (used by glm5_token / hf_token); "
                        "e.g. models/qwen_v3_tokenizer, models/qwen_v35_tokenizer")
    p.add_argument("--chat-mode", type=str, default="wrap_user",
                   choices=["raw", "wrap_user", "messages"],
                   help="chat template wrapping (used by glm5_token / hf_token)")
    p.add_argument("--max-decoded-blocks", type=int, default=None,
                   help="Cap decoded blocks per chain (default: full chain)")
    # v2 (per_user_chains_html_redesign.md): spike detection config
    p.add_argument("--spike-window-min", type=int, default=DEFAULT_SPIKE_WINDOW_MIN,
                   help="Spike detection window in minutes (default: 5)")
    p.add_argument("--spike-threshold", type=float, default=DEFAULT_SPIKE_THRESHOLD,
                   help="Spike trigger multiplier: bucket[i]/bucket[i-1] (default: 5.0)")
    p.add_argument("--no-threshold-sweep", action="store_true",
                   help="Skip 21-point threshold sweep (default: run sweep)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    csv_files = discover_csv_files(args.raw_csv)
    if not csv_files:
        print(f"No CSV files found at {args.raw_csv}", file=sys.stderr)
        sys.exit(1)
    print(f"Input: {len(csv_files)} CSV file(s) under {args.raw_csv}", flush=True)

    # Step 1.6: build encoder (byte = current baseline, glm5_token = 精确化)
    encoder, encoder_meta = build_encoder_from_args(args)
    print(f"Encoder: {encoder_meta['name']} (block_size={args.block_size} {encoder_meta['block_unit']}, "
          f"chat_mode={encoder_meta['chat_mode']})", flush=True)

    # ---- Phase 1: build global trie + per-user tries + v2 metrics in one pass ----
    t0 = time.time()
    global_root = TrieNode()
    user_roots: dict[str, TrieNode] = defaultdict(TrieNode)
    user_request_count: dict[str, int] = defaultdict(int)
    n_total = 0
    n_blocks_total = 0
    n_empty = 0

    # v2 metrics accumulators (single-pass)
    global_seen_keys: set[bytes] = set()
    user_seen_keys: dict[str, set[bytes]] = defaultdict(set)
    user_hit_blocks: dict[str, int] = defaultdict(int)
    user_total_blocks: dict[str, int] = defaultdict(int)
    global_hit_blocks = 0
    new_unique_per_sec: dict[int, int] = defaultdict(int)
    req_per_min: dict[int, int] = defaultdict(int)
    req_per_window: dict[int, int] = defaultdict(int)   # for spike detection
    earliest_ts: Optional[int] = None
    latest_ts: Optional[int] = None
    spike_bucket_secs = args.spike_window_min * 60

    for request_id, user_id, raw_prompt, ts in iter_raw_records(csv_files):
        n_total += 1
        user_request_count[user_id] += 1

        # Parse timestamp (str → int seconds, floor)
        try:
            ts_int = int(float(ts))
        except (ValueError, TypeError):
            ts_int = 0
        if earliest_ts is None:
            earliest_ts = ts_int
        latest_ts = ts_int
        req_per_min[ts_int // 60] += 1
        req_per_window[ts_int // spike_bucket_secs] += 1

        if not raw_prompt:
            n_empty += 1
            global_root.count += 1
            user_roots[user_id].count += 1
            continue

        # Step 1.6: delegate Layer 2-4 (chat_template + tokenize + hash chain)
        # to encoder. byte encoder == split_blocks + compute_prefix_path_keys.
        keys = encoder.encode(raw_prompt)
        n_blocks_total += len(keys)
        user_total_blocks[user_id] += len(keys)

        # v2: compute global LCP (cumulative hit count for ideal_hit_rate_aggregate)
        # hash-chain property: key_i seen ⟹ key_0..key_{i-1} all seen → single scan
        g_lcp = 0
        for k in keys:
            if k in global_seen_keys:
                g_lcp += 1
            else:
                break
        global_hit_blocks += g_lcp

        # v2: compute per-user LCP (user-internal ideal_hit_rate)
        u_seen = user_seen_keys[user_id]
        u_lcp = 0
        for k in keys:
            if k in u_seen:
                u_lcp += 1
            else:
                break
        user_hit_blocks[user_id] += u_lcp

        # v2: track new global unique blocks (for new_unique_blocks_per_second)
        for k in keys:
            if k not in global_seen_keys:
                global_seen_keys.add(k)
                new_unique_per_sec[ts_int] += 1
            u_seen.add(k)

        trie_insert(global_root, keys, request_id)
        trie_insert(user_roots[user_id], keys, request_id)

        if n_total % 1000 == 0:
            print(f"  {n_total:>7,} requests  ({len(user_roots)} users)", flush=True)

    build_s = time.time() - t0
    print(
        f"Built global + {len(user_roots)} per-user tries "
        f"({n_total:,} requests, {n_blocks_total:,} blocks) in {build_s:.1f}s "
        f"(empty prompts: {n_empty})",
        flush=True,
    )

    # v2: compute derived metrics
    ideal_hit_rate_aggregate = (
        global_hit_blocks / n_blocks_total if n_blocks_total else 0.0
    )
    trace_duration_seconds = (
        (latest_ts or 0) - (earliest_ts or 0) if (earliest_ts and latest_ts) else 0
    )
    trace_duration_minutes = trace_duration_seconds / 60.0 if trace_duration_seconds else 0.0
    print(
        f"v2 metrics: ideal_hit_rate_aggregate={ideal_hit_rate_aggregate:.4f}, "
        f"trace_duration={trace_duration_minutes:.1f} min, "
        f"global_unique={len(global_seen_keys):,}",
        flush=True,
    )

    # v2: detect traffic spikes (5 min × 5× by default)
    traffic_spikes = detect_traffic_spikes(
        req_per_window,
        threshold_multiplier=args.spike_threshold,
        window_minutes=args.spike_window_min,
    )
    if traffic_spikes:
        print(
            f"v2 metrics: {len(traffic_spikes)} traffic spike(s) detected "
            f"at ≥ {args.spike_threshold}× over {args.spike_window_min}min windows",
            flush=True,
        )
    else:
        print(
            f"v2 metrics: no ≥{args.spike_threshold}× spike (window={args.spike_window_min} min)",
            flush=True,
        )

    # ---- Phase 2: find LCP for global + each user ----
    t1 = time.time()
    global_chain, global_branch_points = find_lcp(
        global_root, args.branch_threshold, args.coverage_threshold,
    )
    global_keys = [k for k, _ in global_chain]

    # Build per-user chain results (without decoded content yet)
    user_results = []
    decode_request_ids: dict[str, int] = {}  # rid -> max blocks to decode

    for user_id, root in user_roots.items():
        chain, branch_points = find_lcp(
            root, args.branch_threshold, args.coverage_threshold,
        )
        chain_keys = [k for k, _ in chain]
        prefix_match = common_prefix_length(chain_keys, global_keys)
        same_as_global = (chain_keys == global_keys)

        sample_id = None
        if chain:
            sample_id = find_sample_request_at_depth(root, chain)
            if sample_id:
                n_decode = (
                    len(chain) if args.max_decoded_blocks is None
                    else min(len(chain), args.max_decoded_blocks)
                )
                # Track max blocks needed per request_id
                decode_request_ids[sample_id] = max(
                    decode_request_ids.get(sample_id, 0), n_decode
                )

        user_results.append({
            "_user_id": user_id,
            "_chain_keys": chain_keys,
            "_chain_counts": [c for _, c in chain],
            "_branch_points": branch_points,
            "_sample_request_id": sample_id,
            "_prefix_match_with_global": prefix_match,
            "_same_as_global": same_as_global,
        })

    # Add global decode request
    global_sample_id = None
    if global_chain:
        global_sample_id = find_sample_request_at_depth(global_root, global_chain)
        if global_sample_id:
            n_decode = (
                len(global_chain) if args.max_decoded_blocks is None
                else min(len(global_chain), args.max_decoded_blocks)
            )
            decode_request_ids[global_sample_id] = max(
                decode_request_ids.get(global_sample_id, 0), n_decode
            )

    print(
        f"Found {len(global_chain)}-block global chain + "
        f"{len(user_results)} per-user chains  ({time.time()-t1:.2f}s)",
        flush=True,
    )

    # ---- Phase 2.5: threshold sweep (v2, per_user_chains_html_redesign.md §5.7) ----
    # Re-run find_lcp at 21 different branch_thresholds to produce the sweep curve.
    # Trie is already built; this is fast (just walks max-child each time).
    threshold_sweep_points = []
    if not args.no_threshold_sweep:
        t_sweep = time.time()
        for thr in SWEEP_THRESHOLDS:
            sweep_chain, _ = find_lcp(
                global_root, thr, args.coverage_threshold,
            )
            threshold_sweep_points.append({
                "threshold":    thr,
                "chain_length": len(sweep_chain),
            })
        print(
            f"Threshold sweep: 21 points (0.00 → 1.00 step 0.05)  "
            f"({time.time()-t_sweep:.2f}s)",
            flush=True,
        )

    # ---- Phase 3: batch decode all needed chain contents in one CSV pass ----
    t2 = time.time()
    decoded_map = batch_decode(csv_files, decode_request_ids, args.block_size)
    print(
        f"Decoded {len(decoded_map)}/{len(decode_request_ids)} chain samples "
        f"({time.time()-t2:.2f}s)",
        flush=True,
    )

    # ---- Phase 4: assemble output ----
    def build_lcp_content(keys, counts, denom: int, decoded: list[str]) -> list[dict]:
        return [
            {
                "position": i,
                "prefix_path_key": k.hex(),
                "count": c,
                "coverage_pct": round(c / denom * 100, 3) if denom else 0.0,
                "decoded_text": decoded[i] if i < len(decoded) else None,
            }
            for i, (k, c) in enumerate(zip(keys, counts))
        ]

    # Sort users by request count desc
    user_results.sort(key=lambda u: -user_request_count[u["_user_id"]])

    users_out = []
    for u in user_results:
        uid = u["_user_id"]
        rcount = user_request_count[uid]
        chain_keys = u["_chain_keys"]
        chain_counts = u["_chain_counts"]
        sample_id = u["_sample_request_id"]
        decoded = decoded_map.get(sample_id, []) if sample_id else []

        # v2: per-user hit_rate + request_pct (§9.2.2 用户偏斜表用)
        u_total = user_total_blocks[uid]
        u_hit_rate = user_hit_blocks[uid] / u_total if u_total else 0.0
        u_req_pct = rcount / n_total * 100 if n_total else 0.0

        users_out.append({
            "user_id": uid,
            "request_count": rcount,
            "request_pct": round(u_req_pct, 3),                    # v2
            "ideal_hit_rate": round(u_hit_rate, 6),                # v2
            "total_blocks": u_total,                               # v2
            "hit_blocks": user_hit_blocks[uid],                    # v2
            "chain_length": len(chain_keys),
            "chain_coverage_count": chain_counts[-1] if chain_counts else 0,
            "chain_coverage_pct": (
                chain_counts[-1] / rcount * 100 if (chain_counts and rcount) else 0.0
            ),
            "prefix_match_with_global": u["_prefix_match_with_global"],
            "same_as_global": u["_same_as_global"],
            "sample_request_id": sample_id,
            "lcp_content": build_lcp_content(chain_keys, chain_counts, rcount, decoded),
            "branch_points": u["_branch_points"],
        })

    # v2: reuse_inversion_ratio + max/min hit user (per_user_chains_html_redesign §5.2)
    if users_out:
        hit_rates = [(u["user_id"], u["ideal_hit_rate"]) for u in users_out]
        max_hit_uid, max_hit = max(hit_rates, key=lambda kv: kv[1])
        min_hit_uid, min_hit = min(hit_rates, key=lambda kv: kv[1])
        if min_hit > 0:
            inv_ratio_raw = max_hit / min_hit
            inv_ratio = round(inv_ratio_raw, 2)
            inv = inv_ratio_raw >= 2.0
        elif max_hit > 0:
            inv_ratio = "inf (min hit_rate = 0)"
            inv = True
        else:
            inv_ratio = 1.0
            inv = False
    else:
        max_hit_uid = min_hit_uid = None
        max_hit = min_hit = 0.0
        inv_ratio = 1.0
        inv = False

    # User aggregate stats
    n_users = len(users_out)
    n_with_chain = sum(1 for u in users_out if u["chain_length"] > 0)
    n_match_full = sum(1 for u in users_out if u["same_as_global"])
    n_match_50pct = sum(
        1 for u in users_out
        if len(global_keys) > 0
        and u["prefix_match_with_global"] >= 0.5 * len(global_keys)
    )
    n_unique_chain = sum(
        1 for u in users_out
        if u["chain_length"] > 0 and u["prefix_match_with_global"] == 0
    )

    # Global chain content
    global_decoded = decoded_map.get(global_sample_id, []) if global_sample_id else []
    global_counts = [c for _, c in global_chain]

    # v2: build time_series + quantile dicts
    req_per_min_series = [
        {"minute": m, "count": req_per_min[m]} for m in sorted(req_per_min)
    ]
    new_per_sec_series = [
        {"second": s, "count": new_unique_per_sec[s]}
        for s in sorted(new_unique_per_sec)
    ]
    cumulative_unique: list[dict] = []
    running = 0
    for entry in new_per_sec_series:
        running += entry["count"]
        cumulative_unique.append({"second": entry["second"], "total": running})

    # v2 fix (2026-05-14): pad zero-buckets so quantiles reflect the full trace,
    # not only the seconds/minutes that happened to have new blocks. Otherwise
    # a sparse trace (e.g., 15 active seconds out of 335) inflates p50/p80/p95
    # because the (300+) zero seconds are missing from the input series.
    total_seconds = 0
    total_minutes = 0
    if earliest_ts is not None and latest_ts is not None and latest_ts >= earliest_ts:
        total_seconds = max(latest_ts - earliest_ts + 1, len(new_per_sec_series))
        total_minutes = max(
            (latest_ts // 60) - (earliest_ts // 60) + 1,
            len(req_per_min_series),
        )

    req_per_min_q = quantile_summary(
        [m["count"] for m in req_per_min_series],
        total_count=total_minutes,
    )
    new_per_sec_q = quantile_summary(
        [s["count"] for s in new_per_sec_series],
        total_count=total_seconds,
    )

    output = {
        "input": str(args.raw_csv),
        "input_files": [str(p) for p in csv_files],
        "encoder_meta": encoder_meta,  # Step 1.6
        "params": {
            "branch_threshold": args.branch_threshold,
            "coverage_threshold": args.coverage_threshold,
            "block_size": args.block_size,
        },
        "stats": {
            "total_requests": n_total,
            "total_users": n_users,
            "total_blocks": n_blocks_total,
            "empty_prompts": n_empty,
            "build_seconds": round(build_s, 2),
            # v2 metrics
            "ideal_hit_rate_aggregate": round(ideal_hit_rate_aggregate, 6),
            "global_hit_blocks": global_hit_blocks,
            "global_unique_blocks": len(global_seen_keys),
            "trace_duration_seconds": trace_duration_seconds,
            "trace_duration_minutes": round(trace_duration_minutes, 2),
            "earliest_timestamp": earliest_ts,
            "latest_timestamp": latest_ts,
        },
        "user_aggregate": {
            "users_with_chain": n_with_chain,
            "users_with_no_chain": n_users - n_with_chain,
            "users_matching_global_fully": n_match_full,
            "users_matching_global_50pct_prefix": n_match_50pct,
            "users_with_unique_chain": n_unique_chain,
        },
        # v2: reuse_inversion signals (top-level, per_user_chains_html_redesign §5.2)
        "reuse_inversion_ratio": inv_ratio,
        "reuse_inversion":       inv,
        "max_hit_user":          max_hit_uid,
        "min_hit_user":          min_hit_uid,
        "max_hit_rate":          round(max_hit, 4),
        "min_hit_rate":          round(min_hit, 4),
        # v2: time series + quantile (§5.3 / §5.5 / §5.6)
        "time_series": {
            "requests_per_minute":          req_per_min_series,
            "new_unique_blocks_per_second": new_per_sec_series,
            "cumulative_unique_blocks":     cumulative_unique,
        },
        "requests_per_min_q":          req_per_min_q,
        "new_unique_blocks_per_sec_q": new_per_sec_q,
        # v2: traffic spikes (§5.4)
        "traffic_spikes": traffic_spikes,
        "spike_config": {
            "window_minutes":       args.spike_window_min,
            "threshold_multiplier": args.spike_threshold,
        },
        # v2: threshold sweep (§5.7)
        "threshold_sweep_points": threshold_sweep_points,
        "global_chain": {
            "chain_length": len(global_chain),
            "chain_coverage_count": global_counts[-1] if global_counts else 0,
            "chain_coverage_pct": (
                global_counts[-1] / n_total * 100 if (global_counts and n_total) else 0.0
            ),
            "sample_request_id": global_sample_id,
            "lcp_content": build_lcp_content(
                global_keys, global_counts, n_total, global_decoded,
            ),
            "branch_points": global_branch_points,
        },
        "users": users_out,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # ---- Console summary ----
    print("\n=== Result ===", flush=True)
    print(f"  total requests       : {n_total:,}", flush=True)
    print(f"  total users          : {n_users}", flush=True)
    print(f"  ideal_hit_rate_aggr  : {ideal_hit_rate_aggregate:.4f}", flush=True)
    print(
        f"  reuse_inversion      : ratio={inv_ratio}  "
        f"max=({max_hit_uid}, {max_hit:.4f})  min=({min_hit_uid}, {min_hit:.4f})",
        flush=True,
    )
    print(
        f"  req/min quantile     : "
        f"avg={req_per_min_q['avg']} p50={req_per_min_q['p50']} "
        f"p80={req_per_min_q['p80']} p95={req_per_min_q['p95']} "
        f"max={req_per_min_q['max']}",
        flush=True,
    )
    print(
        f"  new_block/s quantile : "
        f"avg={new_per_sec_q['avg']} p50={new_per_sec_q['p50']} "
        f"p80={new_per_sec_q['p80']} p95={new_per_sec_q['p95']} "
        f"max={new_per_sec_q['max']}",
        flush=True,
    )
    print(f"  traffic spikes       : {len(traffic_spikes)}", flush=True)
    print(f"  threshold sweep      : {len(threshold_sweep_points)} points", flush=True)
    print(f"  global chain length  : {len(global_chain)}", flush=True)
    if global_chain:
        cov = global_counts[-1] / n_total * 100
        print(f"  global chain coverage: {global_counts[-1]:,}/{n_total:,}  ({cov:.1f}%)",
              flush=True)
    print(f"\n  Users with chain     : {n_with_chain}/{n_users}", flush=True)
    print(f"  Users matching global fully     : {n_match_full}", flush=True)
    print(f"  Users matching ≥50%-prefix      : {n_match_50pct}", flush=True)
    print(f"  Users with unique (non-global)  : {n_unique_chain}", flush=True)

    # Per-user table (top 30)
    print("\n  Per-user chain summary (sorted by request count):", flush=True)
    print(
        f"    {'user_id':<32} {'reqs':>8} {'chain':>6} {'cov%':>6} {'vs_global':>12}",
        flush=True,
    )
    print(f"    {'-'*32} {'-'*8} {'-'*6} {'-'*6} {'-'*12}", flush=True)
    for u in users_out[:30]:
        if u["same_as_global"]:
            tag = "= global"
        elif u["chain_length"] == 0:
            tag = "(no chain)"
        elif u["prefix_match_with_global"] == 0:
            tag = "unique"
        else:
            tag = f"prefix:{u['prefix_match_with_global']}"
        uid_disp = u["user_id"] if len(u["user_id"]) <= 32 else u["user_id"][:29] + "..."
        print(
            f"    {uid_disp:<32} {u['request_count']:>8,} "
            f"{u['chain_length']:>6} {u['chain_coverage_pct']:>5.1f}% {tag:>12}",
            flush=True,
        )
    if len(users_out) > 30:
        print(f"    ... ({len(users_out)-30} more)", flush=True)

    print(f"\n  output → {args.output}", flush=True)


if __name__ == "__main__":
    main()
