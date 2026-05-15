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
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
# Step 1.6: lib/ for PromptEncoder strategies
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from verify_chain_path_closure import (  # noqa: E402
    TrieNode,
    compute_prefix_path_keys,
    discover_csv_files,
    iter_raw_records,
    split_blocks,
    trie_insert,
)
from lib.prompt_encoder import build_encoder_from_args  # noqa: E402
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

# v2 classification thresholds — overridable via CLI flags
DEFAULT_HIT_LOW = 0.30
DEFAULT_HIT_HIGH = 0.60
DEFAULT_COV_LOW = 0.10
DEFAULT_COV_HIGH = 0.50
DEFAULT_CHAIN_LEN_RATIO_LONG = 0.30
DEFAULT_UNIQUE_SHARE_LOW = 0.05
DEFAULT_UNIQUE_SHARE_HIGH = 0.30
DEFAULT_CHAIN_COUNT_MANY = 3
DEFAULT_SPIKE_WINDOW_MIN = 5
DEFAULT_SPIKE_THRESHOLD = 5.0


# ---------------------------------------------------------------------------
# Pass 1: per-user request counts + req-per-5min bucket for spike detection
# ---------------------------------------------------------------------------

def collect_user_counts(
    csv_files: list[Path], window_minutes: int = 5,
) -> tuple[dict[str, int], int, dict[int, int], dict[int, int], int, int, int]:
    """Single pass: counts requests per user, model-level time series, spike bucket.

    Returns:
        (per_user_counts, total_requests, req_per_window_bucket,
         model_req_per_min, ts_parse_failed_count, earliest_ts, latest_ts)

    Bucket index for spike: floor(ts / (window_minutes * 60)).
    `model_req_per_min` aggregates *all* users for HTML §1 model-level quantile.
    `ts_parse_failed_count` is the count of rows where timestamp parse failed
    (fallback to 0) — feeds the rpm_avg=0 bug diagnostic.
    """
    counts: dict[str, int] = defaultdict(int)
    req_per_window: dict[int, int] = defaultdict(int)
    model_req_per_min: dict[int, int] = defaultdict(int)
    total = 0
    bucket_secs = window_minutes * 60
    ts_parse_failed = 0
    earliest_ts: Optional[int] = None
    latest_ts: Optional[int] = None

    for _rid, user_id, _prompt, ts in iter_raw_records(csv_files):
        counts[user_id] += 1
        try:
            ts_int = int(float(ts))
        except (ValueError, TypeError):
            ts_int = 0
            ts_parse_failed += 1
        req_per_window[ts_int // bucket_secs] += 1
        model_req_per_min[ts_int // 60] += 1
        if earliest_ts is None or ts_int < earliest_ts:
            earliest_ts = ts_int
        if latest_ts is None or ts_int > latest_ts:
            latest_ts = ts_int
        total += 1
    return (
        dict(counts), total, dict(req_per_window), dict(model_req_per_min),
        ts_parse_failed, earliest_ts or 0, latest_ts or 0,
    )


def detect_traffic_spikes(
    req_per_window: dict[int, int],
    threshold_multiplier: float = 5.0,
    window_minutes: int = 5,
) -> list[dict]:
    """Detect adjacent 5-min windows where req_count[i] >= req_count[i-1] * threshold.

    Buckets are merged into contiguous spans (consecutive bucket indexes).
    Returns list of spike events sorted by window_start_bucket.
    """
    if not req_per_window:
        return []
    sorted_buckets = sorted(req_per_window.items())
    spikes: list[dict] = []
    for i in range(1, len(sorted_buckets)):
        prev_b, prev_n = sorted_buckets[i - 1]
        cur_b, cur_n = sorted_buckets[i]
        if prev_n > 0 and cur_n / prev_n >= threshold_multiplier:
            spikes.append({
                "window_start_bucket":  cur_b,
                "window_start_seconds": cur_b * window_minutes * 60,
                "prev_window_count":    prev_n,
                "this_window_count":    cur_n,
                "ratio_to_prev":        round(cur_n / prev_n, 2),
            })
    return spikes


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


def _compute_chain_shadow_pairs(
    chains: list[dict],
    min_shared: int = 5,
    min_ratio: float = 0.30,
) -> list[dict]:
    """v3 §8: detect chains with substantial shared prefix.

    "Substantial" = shared_prefix_blocks >= min_shared AND
    (ratio_a >= min_ratio OR ratio_b >= min_ratio).

    Filters out root-level micro-shares (e.g., 1-block system prompt header
    that always overlaps between chains) to keep warning meaningful.
    "chain 数虚高" 警告应该指向 "chain 0 + chain 1 前 100 block 完全一致" 这种.

    Defaults: min_shared=5 blocks, min_ratio=30%. Configurable via CLI flags.
    """
    pairs: list[dict] = []
    for i in range(len(chains)):
        for j in range(i + 1, len(chains)):
            keys_a = [b.get("prefix_path_key") for b in chains[i].get("decoded_content", [])]
            keys_b = [b.get("prefix_path_key") for b in chains[j].get("decoded_content", [])]
            if not keys_a or not keys_b:
                continue
            common = 0
            for k1, k2 in zip(keys_a, keys_b):
                if k1 is not None and k1 == k2:
                    common += 1
                else:
                    break
            if common < min_shared:
                continue
            la = len(keys_a)
            lb = len(keys_b)
            ratio_a = common / la if la else 0
            ratio_b = common / lb if lb else 0
            if ratio_a < min_ratio and ratio_b < min_ratio:
                continue
            pairs.append({
                "chain_a": chains[i].get("chain_id", i),
                "chain_b": chains[j].get("chain_id", j),
                "shared_prefix_blocks": common,
                "chain_a_length": la,
                "chain_b_length": lb,
                "ratio_a": round(ratio_a, 3),
                "ratio_b": round(ratio_b, 3),
            })
    return pairs


def _reuse_time_quantiles(reuse_times: list[int]) -> dict:
    """v3 §6 reuse_time quantile summary."""
    if not reuse_times:
        return {"avg": 0.0, "p50": 0, "p80": 0, "p95": 0, "max": 0, "count": 0}
    sorted_rt = sorted(reuse_times)
    return {
        "avg":   round(sum(sorted_rt) / len(sorted_rt), 2),
        "p50":   percentile_int(sorted_rt, 50),
        "p80":   percentile_int(sorted_rt, 80),
        "p95":   percentile_int(sorted_rt, 95),
        "max":   sorted_rt[-1],
        "count": len(sorted_rt),
    }


def _reuse_time_cdf_log(reuse_times: list[int], n_points: int = 50) -> list[dict]:
    """v3 §6 reuse_time CDF sampled at log-spaced x.

    Returns [{t_seconds, cumulative_pct}] — used by HTML svg_cdf_log_x.
    Log-spaced sampling between 1s and max(reuse_times).
    Floor of log10(0) handled: t_seconds = 0 always cumulative_pct = 0.
    """
    if not reuse_times:
        return []
    sorted_rt = sorted(reuse_times)
    max_rt = sorted_rt[-1]
    n_total = len(sorted_rt)

    # Generate log-spaced t values from 1 to max_rt
    import math
    if max_rt <= 1:
        ts_grid = [0, max_rt]
    else:
        log_max = math.log10(max_rt)
        ts_grid = [0] + [
            max(1, int(round(10 ** (i * log_max / (n_points - 1)))))
            for i in range(n_points)
        ]
        # Dedupe (after rounding)
        ts_grid = sorted(set(ts_grid))

    points = []
    for t in ts_grid:
        # Binary-search-like cumulative count of rt <= t
        cnt = 0
        for rt in sorted_rt:
            if rt <= t:
                cnt += 1
            else:
                break
        points.append({
            "t_seconds": t,
            "cumulative_pct": round(cnt / n_total * 100, 3),
        })
    return points


def lcp_histogram(lcps: list[int]) -> tuple[list[dict], dict, list[dict]]:
    """Equal-width histogram with ~30 buckets.

    Returns: (histogram, quantiles_dict, top10_lcp_values)
      - histogram:    [{lcp_low, lcp_high, count}] equal-width buckets
      - quantiles:    {p30, p50, p80, p95, max, bucket_size}  (v3: add p30 + p80)
      - top10:        [{lcp_value, request_count}] most common LCP values
    """
    if not lcps:
        return ([],
                {"p30": 0, "p50": 0, "p80": 0, "p95": 0, "max": 0, "bucket_size": 1},
                [])
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
        "p30": percentile_int(sorted_lcps, 30),
        "p50": percentile_int(sorted_lcps, 50),
        "p80": percentile_int(sorted_lcps, 80),
        "p95": percentile_int(sorted_lcps, 95),
        "max": max_lcp,
        "bucket_size": bucket_size,
    }
    # v3 §7: Top-10 LCP values (raw request counts at each LCP value)
    lcp_counter = Counter(lcps)
    top10 = [
        {"lcp_value": lcp_val, "request_count": cnt}
        for lcp_val, cnt in lcp_counter.most_common(10)
    ]
    return histogram, quantiles, top10


def _infer_business_type(chains: list[dict]) -> tuple[str, str]:
    """Heuristic business-type inference from decoded chain content.

    Returns (type_id, evidence_snippet). Type_id matches the categories used
    in docs/step3_algorithm_decision_matrix.md §1.

    Heuristics are intentionally conservative — when ambiguous, returns
    "unknown" so a human can disambiguate via HTML §5 chain forest.
    """
    if not chains:
        return ("none", "no chains")

    # Sample text from top chain
    head = chains[0]
    text = "".join(
        (b.get("decoded_text") or "") for b in head.get("decoded_content", [])
    )[:1000]
    n_chains = len(chains)
    top_cov = head.get("coverage_pct", 0.0)
    top_len = head.get("chain_length", 0)

    text_lower = text.lower()

    # Agent / Claude Code style: JSON tool schema
    if ('"tools":' in text_lower or '"function":' in text_lower) and \
       ('"parameters"' in text_lower or '"$schema"' in text_lower):
        return ("agent_tools", "JSON tool schema detected in chain[0]")

    # Multi-task router: "你是XX助手" with several independent chains
    if n_chains >= 3 and ("你是" in text or "你扮演" in text or "助手" in text):
        return ("router", f"'你是...助手' style + {n_chains} independent chains")

    # RAG / generation: keywords + 2-3 chains with mid-length
    rag_kw = ("文档" in text or "RAG" in text or "markdown" in text_lower
              or "检索" in text or "总结" in text)
    if rag_kw and 2 <= n_chains <= 6:
        return ("rag", "RAG/document keywords + multi-chain structure")

    # Classification: short chain + classification keywords
    if top_len < 30 and ("分类" in text or "归类" in text or "类别" in text):
        return ("classification", "short chain + classification keywords")

    # Long-doc reuse: very short global chain but the user has high hit_rate
    # (this is judged at recommendation time using hit_rate, not here)
    if n_chains == 1 and top_len < 30:
        return ("short_chain_unknown", "single short chain — check hit_rate")

    return ("unknown", "no heuristic match")


def _estimate_uplift(
    primary: str, hit_rate: float, top_cov_pct: float, n_chains: int,
    request_pct: float,
) -> dict:
    """Order-of-magnitude uplift estimate for the primary algorithm.

    These are heuristics from Step 1 data only; Step 2 measurement is needed
    for confident numbers. confidence ∈ {"low", "medium"}.
    """
    if primary == "D":
        # Prompt-rewrite — totally business-dependent
        return {"kind": "hit_rate", "value": "uncertain; +0 to +15pp depending on rewrite scope",
                "confidence": "low"}
    if primary == "A":
        # Routing — depends on batch behavior; Step 2 dependency
        return {"kind": "ttft", "value": "+10–30% TTFT reduction (batch internal hits)",
                "confidence": "low"}
    if primary == "C":
        # Capacity / pooling — proportional to capacity until WS plateau
        return {"kind": "hit_rate", "value": "ceiling approaches ideal_hit_rate; "
                f"needs cache ≥ unique_blocks to fully realize",
                "confidence": "medium"}
    # B — chain pin
    # Upper bound: if user currently misses chain portion, pin recovers
    # roughly top_cov × (1 − hit_rate) percentage points of hit rate.
    if top_cov_pct > 0:
        bound = (top_cov_pct / 100.0) * (1.0 - hit_rate) * 100.0
        bound_str = f"+{bound:.1f}pp upper bound (chain pin recovers chain-aligned misses)"
    else:
        bound_str = "uncertain (no chain dominant)"
    return {"kind": "hit_rate", "value": bound_str, "confidence": "medium"}


def _make_reasons(
    hit_rate: float, n_chains: int, top_cov_pct: float, top_len: int,
    unique_blocks: int, new_per_sec_p95: int, business: str,
) -> list[str]:
    """Human-readable bullet list explaining the primary recommendation."""
    reasons = []
    reasons.append(f"ideal_hit_rate = {hit_rate:.3f}")
    if n_chains == 0:
        reasons.append("chain forest 为空 — chain pin 完全无杠杆")
    else:
        reasons.append(
            f"chain forest: {n_chains} 条, dominant_chain length={top_len} block / "
            f"cov={top_cov_pct:.1f}%"
        )
    if unique_blocks >= 1_000_000:
        reasons.append(f"unique_blocks {unique_blocks:,} ≥ 1M, cache 容量先于 pin")
    elif unique_blocks >= 200_000:
        reasons.append(f"unique_blocks {unique_blocks:,} 较大, 容量需关注")
    if new_per_sec_p95 >= 500:
        reasons.append(f"insertion rate p95 = {new_per_sec_p95}/s 极高, 路由分流可能必要")
    if business != "unknown":
        reasons.append(f"业务类型推断: {business}")
    return reasons


def compute_model_context(
    user_reports: dict[str, dict],
    traffic_spikes: list[dict] | None = None,
    spike_config: dict | None = None,
    n_users_total: int | None = None,
    ts_parse_failed_count: int = 0,
    requests_per_min_q: dict | None = None,
    new_unique_blocks_per_sec_q: dict | None = None,
) -> dict:
    """Per-model cross-user signals + v3 aggregate metrics.

    `n_users_total` is the count of distinct user_ids in the whole model
    (from pass-1 collect_user_counts), distinct from `n_users` which is
    the count of Top-K selected users analyzed in detail.

    `ts_parse_failed_count` and `trace_duration_caveat` help diagnose the
    rpm_avg=0 / unique_rpm_avg=0 bug (see user_report_html_redesign.md §4.1).
    """
    hit_rates = []
    for r in user_reports.values():
        h = (r.get("stats") or {}).get("ideal_hit_rate", 0.0)
        hit_rates.append(h)

    if not hit_rates:
        return {
            "n_users": 0,
            "n_users_total": n_users_total or 0,
            "is_multi_tenant": False,
            "max_hit_rate": 0.0,
            "min_hit_rate": 0.0,
            "reuse_inversion_ratio": 1.0,
            "reuse_inversion": False,
            "ideal_hit_rate_aggregate": 0.0,
            "rpm_avg": 0.0,
            "unique_rpm_avg": 0.0,
            "total_unique_blocks_topk": 0,
            "trace_duration_minutes": 0.0,
            "trace_duration_caveat": "no selected users",
            "ts_parse_failed_count": ts_parse_failed_count,
            "requests_per_min_q": requests_per_min_q or {},
            "new_unique_blocks_per_sec_q": new_unique_blocks_per_sec_q or {},
            "traffic_spikes": traffic_spikes or [],
            "spike_config": spike_config or {},
            "model_params_class": None,
            "instance_count": None,
            "cache_capacity_blocks": None,
        }

    max_h = max(hit_rates)
    min_h = min(hit_rates)
    if min_h > 0:
        ratio = max_h / min_h
        inversion = ratio >= 2.0
    elif max_h > 0:
        ratio = float("inf")
        inversion = True
    else:
        ratio = 1.0
        inversion = False
    n_users = len(user_reports)

    # v2 aggregate metrics
    total_requests = sum((r.get("stats") or {}).get("total_requests", 0)
                         for r in user_reports.values())
    total_blocks = sum((r.get("stats") or {}).get("total_blocks", 0)
                       for r in user_reports.values())
    total_hit_blocks = sum((r.get("stats") or {}).get("hit_blocks", 0)
                           for r in user_reports.values())
    # Top-K sum is an upper bound — long-tail users not collected, so this
    # over-counts overlap if any. Used as denominator for share_of_model_unique.
    total_unique_topk = sum((r.get("stats") or {}).get("unique_blocks", 0)
                            for r in user_reports.values())

    ideal_hit_rate_aggregate = (
        total_hit_blocks / total_blocks if total_blocks else 0.0
    )

    earliest_list = [(r.get("stats") or {}).get("earliest_timestamp")
                     for r in user_reports.values()]
    latest_list = [(r.get("stats") or {}).get("latest_timestamp")
                   for r in user_reports.values()]
    earliest_list = [e for e in earliest_list if e]
    latest_list = [l for l in latest_list if l]
    if earliest_list and latest_list:
        duration_sec = max(latest_list) - min(earliest_list)
    else:
        duration_sec = 0
    duration_min = duration_sec / 60.0 if duration_sec else 0.0

    rpm_avg = total_requests / duration_min if duration_min else 0.0
    unique_rpm_avg = total_unique_topk / duration_min if duration_min else 0.0

    # v3: trace_duration_caveat — flag rpm_avg=0/unique_rpm_avg=0 root cause
    caveat = None
    if duration_sec == 0:
        if not earliest_list or not latest_list:
            caveat = "no valid timestamps in any selected user (all ts parse failed?)"
        else:
            caveat = "trace_duration = 0 (earliest_ts == latest_ts); rpm/unique_rpm 不可靠"
    elif ts_parse_failed_count > 0:
        caveat = f"{ts_parse_failed_count} timestamp(s) failed to parse (fallback to 0); rpm/unique_rpm 可能偏低"

    return {
        "n_users": n_users,
        # v3: n_users_total = 全模型 user 数 (from pass-1 collect_user_counts)
        "n_users_total": n_users_total if n_users_total is not None else n_users,
        # Multi-tenant when ≥3 users share the model (1-2 users still has
        # cross-user driver but doesn't justify a router layer)
        "is_multi_tenant": n_users >= 3,
        "max_hit_rate": round(max_h, 4),
        "min_hit_rate": round(min_h, 4),
        # Reuse inversion: max/min hit_rate ratio ≥ 2.0 indicates that
        # some users have radically different cache behavior from others,
        # which means shared LRU will let the low-reuse users evict the
        # high-reuse users' chains. Per-user routing fixes this.
        "reuse_inversion_ratio": (
            round(ratio, 2) if ratio != float("inf") else "inf (min hit_rate = 0)"
        ),
        "reuse_inversion": inversion,
        # v2 aggregate
        "ideal_hit_rate_aggregate": round(ideal_hit_rate_aggregate, 6),
        "rpm_avg":          round(rpm_avg, 4),
        "unique_rpm_avg":   round(unique_rpm_avg, 4),
        "total_unique_blocks_topk": total_unique_topk,
        "trace_duration_minutes": round(duration_min, 2),
        # v3: bug-diagnostic fields
        "trace_duration_caveat": caveat,
        "ts_parse_failed_count": ts_parse_failed_count,
        # v3: model-level quantile (for HTML reference lines + user vs model 对比)
        "requests_per_min_q": requests_per_min_q or {},
        "new_unique_blocks_per_sec_q": new_unique_blocks_per_sec_q or {},
        # Traffic spike detection (filled by main from detect_traffic_spikes)
        "traffic_spikes":   traffic_spikes or [],
        "spike_config":     spike_config or {},
        # Manual-input fields (HTML red-flag "缺失" when None)
        "model_params_class":   None,
        "instance_count":       None,
        "cache_capacity_blocks": None,
    }


def compute_classifications(
    user_stats: dict,
    chain_forest_summary: dict,
    thresholds: dict,
) -> dict:
    """6-dim categorical classification per matrix §9.2.2.

    Returns: dict with hit_band / cov_band / chain_len_band /
    unique_share_band / chain_count_band + is_anomaly flag.
    """
    hit = user_stats.get("ideal_hit_rate", 0.0) or 0.0
    cov_pct = chain_forest_summary.get("dominant_chain_coverage_pct", 0.0) or 0.0
    cov = cov_pct / 100.0  # convert pct to fraction
    chain_len_ratio = chain_forest_summary.get("chain_length_ratio", 0.0) or 0.0
    chain_count = chain_forest_summary.get("total_chains", 0) or 0
    share = user_stats.get("share_of_model_unique")
    # share may be None if model context not computed yet; default to "normal"
    share_val = share if share is not None else 0.0

    def band(value, lo, hi, low_label="low", high_label="high", mid_label="normal"):
        if value < lo:
            return low_label
        if value > hi:
            return high_label
        return mid_label

    hit_band  = band(hit, thresholds["hit_low"], thresholds["hit_high"])
    cov_band  = band(cov, thresholds["cov_low"], thresholds["cov_high"])
    chain_len_band = (
        "long" if chain_len_ratio > thresholds["chain_len_ratio_long"] else "short"
    )
    unique_share_band = band(
        share_val,
        thresholds["unique_share_low"], thresholds["unique_share_high"],
    )
    chain_count_band = (
        "many" if chain_count >= thresholds["chain_count_many"] else "few"
    )

    # is_anomaly: long chain + low cov + low hit (long chain that doesn't reuse)
    is_anomaly = (
        chain_len_band == "long"
        and cov_band == "low"
        and hit_band == "low"
    )

    return {
        "hit_band":           hit_band,
        "cov_band":           cov_band,
        "chain_len_band":     chain_len_band,
        "unique_share_band":  unique_share_band,
        "chain_count_band":   chain_count_band,
        "is_anomaly":         is_anomaly,
    }


def _select_a_subtype(
    params_class: str | None, n_users: int,
    has_inversion: bool, hit_band: str, unique_share_band: str,
    chain_count_band: str, chain_len_band: str, cov_band: str,
) -> tuple[str, str | None]:
    """Decide A subtype per matrix §9.2.3 A rules. Returns (subtype, annotation)."""
    if params_class == "large_200B_moe":
        return (
            "A(4) 暂缓",
            "待人工补 instance_count + cache_capacity_blocks，再决定 A 路由策略",
        )
    if (n_users >= 3 and has_inversion
            and hit_band == "low" and unique_share_band == "high"):
        return (
            "A(1) isolation routing",
            "低 hit + 高 unique_share 用户隔离，防其驱逐其他用户 chain",
        )
    if (n_users <= 3 and hit_band == "high"
            and unique_share_band == "high"
            and chain_count_band == "many" and chain_len_band == "long"
            and cov_band in ("normal", "low")):
        return (
            "A(2) 多 chain 实例化 + 实例内多队列 LRU",
            "按 chain 拆分到独立实例 + llm-d prefix_score 路由",
        )
    if (n_users <= 3 and hit_band == "high"
            and unique_share_band == "high"
            and chain_count_band == "few" and chain_len_band == "short"
            and cov_band == "high"):
        return (
            "A(3) skill/文档 prefix routing",
            "中段 skill/文档命中，相同 skill 经 prefix_score 路由到同实例",
        )
    return ("A0 baseline (llm-d prefix_score)", None)


def _select_b_subtype(
    n_users: int, unique_share_band: str, chain_count_band: str,
) -> str:
    """Decide B subtype per matrix §9.2.3 B rules. Model-level switch."""
    # Multi-tenant with high-unique user OR many chains anywhere → B(2)
    if (n_users >= 3 and unique_share_band == "high") \
       or (n_users == 1 and chain_count_band == "many") \
       or (n_users >= 3 and chain_count_band == "many"):
        return "B(2) 多队列 LRU（按 user-chain-hash; 淘汰打分 TBD Step 2 实测）"
    return "B(1) 默认 LRU"


def _select_c_subtype(
    b_subtype: str, unique_share_band: str,
    chain_count_band: str, chain_len_band: str,
) -> str | None:
    """Decide C subtype per matrix §9.2.3 C rules."""
    if unique_share_band == "high" and b_subtype.startswith("B(1)"):
        return "C(1) 强池化"
    if chain_count_band == "many" and chain_len_band == "long":
        return "C(2) 弱池化（容量保障）"
    return None


def _make_impl_steps_v2(
    a_subtype: str, b_subtype: str, c_subtype: str | None, primary: str,
    n_chains: int, top_len: int, top_cov_pct: float, unique: int,
) -> list[str]:
    """Per-subtype implementation steps."""
    steps: list[str] = []

    if "A(1)" in a_subtype:
        steps.append("将该 user 路由到独立实例 / 独立 cache 池（物理隔离）")
        steps.append("观察隔离后其他用户的 hit 提升幅度，验证污染假设")
    elif "A(2)" in a_subtype:
        steps.append(f"按 chain ({n_chains} 条) 拆分到独立实例，每实例承担一个长 chain")
        steps.append("配合 llm-d prefix_score 路由相同 chain 请求到同实例")
        steps.append(f"实例内淘汰用 {b_subtype}")
    elif "A(3)" in a_subtype:
        steps.append("部署多实例，按 llm-d prefix_score 自动路由")
        steps.append("可选: 上调 prefix_score 权重 (>2.0) 增强 skill/文档共享")
        steps.append(f"实例内淘汰: {b_subtype}")
    elif "A(4)" in a_subtype:
        steps.append("⚠️ 大模型 (200B+ MOE): 待人工补 instance_count + cache_capacity_blocks")
        steps.append("现阶段先按 llm-d baseline 部署，收集生产 cache 命中数据")
        steps.append("Step 2 实测后，依实例个数 + 容量决定 A 路由策略")
    elif a_subtype.startswith("A0"):
        steps.append("使用 llm-d baseline (prefix_score 权重 2.0)，无需额外路由干预")

    if "B(2)" in b_subtype:
        if n_chains > 0:
            steps.append(f"为该 user 的 {n_chains} 条 chain 各分配独立 LRU 队列")
        steps.append("淘汰打分公式 TBD — Step 2 实测调参（输入: unique_rpm / hit_rate / chain_len）")
    elif "B(1)" in b_subtype:
        steps.append("使用默认 LRU 淘汰（无需多队列改造）")

    if c_subtype:
        if "C(1)" in c_subtype:
            steps.append(f"强池化: 评估 cache 容量是否能 hold {unique:,} unique blocks")
            steps.append("容量不足时: 物理扩容 / KV 量化 (fp8 或 int8) / 跨实例池化")
        elif "C(2)" in c_subtype:
            steps.append("弱池化: 容量保障即可，主要杠杆仍是路由 + LRU")

    if primary == "D":
        steps.append("与业务方沟通: 减少 prompt 动态字段 (request_id / timestamp / seed)")
        steps.append("评估业务上限是否真的低 (考虑放弃 prefix cache 优化)")

    return steps


def compute_step3_recommendation(
    report_stats: dict, chains: list[dict], inter_arrival: dict,
    new_per_sec_q: dict, model_context: dict | None = None,
    classifications: dict | None = None,
) -> dict:
    """Decide subtype recommendations per decision matrix §9.2.3 rules.

    Inputs:
      report_stats:     user's stats dict (with share_of_model_unique filled)
      chains:           user's chain forest
      new_per_sec_q:    new-block-per-second quantiles
      model_context:    cross-user signals + model-level metrics (with
                        manual fields model_params_class / instance_count /
                        cache_capacity_blocks)
      classifications:  v2 categorical bands + is_anomaly (from
                        compute_classifications)

    Output dict combines legacy primary/companion (A/B/C/D for CSV) with
    v2 subtype fields (a_subtype / b_subtype / c_subtype).

    See docs/step3_algorithm_decision_matrix.md §9.2 for the underlying rules.
    """
    hit_rate = report_stats.get("ideal_hit_rate", 0.0)
    unique = report_stats.get("unique_blocks", 0)
    total_reqs = report_stats.get("total_requests", 0)
    new_p95 = new_per_sec_q.get("p95", 0)

    n_chains = len(chains)
    top_cov_pct = chains[0]["coverage_pct"] if chains else 0.0
    top_len = chains[0]["chain_length"] if chains else 0

    business_type, evidence = _infer_business_type(chains)

    ctx = model_context or {}
    cls = classifications or {}

    is_multi = ctx.get("is_multi_tenant", False)
    has_inversion = ctx.get("reuse_inversion", False)
    inversion_ratio = ctx.get("reuse_inversion_ratio")
    n_users_ctx = ctx.get("n_users", 1)
    params_class = ctx.get("model_params_class")

    hit_band         = cls.get("hit_band", "normal")
    cov_band         = cls.get("cov_band", "normal")
    chain_len_band   = cls.get("chain_len_band", "short")
    unique_share_band = cls.get("unique_share_band", "normal")
    chain_count_band  = cls.get("chain_count_band", "few")
    is_anomaly       = cls.get("is_anomaly", False)

    # ===== v2 subtype selection =====
    a_subtype, a_annotation = _select_a_subtype(
        params_class, n_users_ctx, has_inversion,
        hit_band, unique_share_band, chain_count_band,
        chain_len_band, cov_band,
    )
    b_subtype = _select_b_subtype(n_users_ctx, unique_share_band, chain_count_band)
    c_subtype = _select_c_subtype(b_subtype, unique_share_band,
                                  chain_count_band, chain_len_band)

    # ===== Legacy primary/companion (for CSV compatibility) =====
    pin_uplift_pp = (top_cov_pct / 100.0) * (1.0 - hit_rate) * 100.0
    business_ceiling_low = hit_rate < 0.30 and pin_uplift_pp < 5.0

    # Primary flavor = which letter dominates
    if not a_subtype.startswith("A0") and "A(4)" not in a_subtype:
        primary = "A"
    elif business_ceiling_low and (not chains or top_cov_pct < 10):
        primary = "D"
    elif c_subtype == "C(1) 强池化":
        primary = "C"
    elif n_chains > 0:
        primary = "B"
    else:
        primary = "D"

    companion = None
    if primary == "A":
        if business_ceiling_low:
            companion = "D"
        elif n_chains > 0:
            companion = "B"
        elif unique >= 200_000:
            companion = "C"
    elif primary == "B":
        if is_multi:
            companion = "A"
        elif unique >= 200_000:
            companion = "C"
    elif primary == "C":
        if n_chains > 0:
            companion = "B"
        elif is_multi:
            companion = "A"
    elif primary == "D":
        if n_chains > 0:
            companion = "B"

    difficulty_map = {"A": "high", "B": "low", "C": "medium", "D": "high"}
    difficulty = difficulty_map.get(primary, "unknown")

    estimated = _estimate_uplift(primary, hit_rate, top_cov_pct, n_chains,
                                 total_reqs)

    reasons = _make_reasons(
        hit_rate, n_chains, top_cov_pct, top_len, unique, new_p95,
        business_type,
    )

    # v2 subtype reasons
    reasons.append(f"A 子类型: {a_subtype}")
    reasons.append(f"B 子类型: {b_subtype}")
    if c_subtype:
        reasons.append(f"C 子类型: {c_subtype}")
    if is_anomaly:
        reasons.append(
            "⚠️ 反常: 长 chain + 低 cov + 低 hit，建议人工检查 chain decoded 内容 "
            "(可能是 wrapper boilerplate / 业务噪声而非真业务复用)"
        )
    if has_inversion and a_subtype.startswith("A(1)"):
        reasons.append(
            f"模型级复用倒置: hit_rate max/min = {inversion_ratio}x (≥ 2.0 触发 A(1) 隔离)"
        )

    impl_steps = _make_impl_steps_v2(
        a_subtype, b_subtype, c_subtype, primary,
        n_chains, top_len, top_cov_pct, unique,
    )

    return {
        # legacy fields (CSV / 下游兼容)
        "primary_algorithm":  primary,
        "companion_algorithm": companion,
        "business_type":      business_type,
        "business_evidence":  evidence,
        "reasons":            reasons,
        "difficulty":         difficulty,
        "estimated_uplift":   estimated,
        "implementation_steps": impl_steps,
        "model_context_snapshot": {
            "n_users":          n_users_ctx,
            "is_multi_tenant":  is_multi,
            "reuse_inversion":  has_inversion,
            "reuse_inversion_ratio": inversion_ratio,
            "model_params_class":   params_class,
            "instance_count":       ctx.get("instance_count"),
            "cache_capacity_blocks": ctx.get("cache_capacity_blocks"),
        } if model_context else None,
        # v2 subtype fields (per §9.2)
        "a_subtype":          a_subtype,
        "a_annotation":       a_annotation,
        "b_subtype":          b_subtype,
        "c_subtype":          c_subtype,
        "classifications":    classifications,
        "is_anomaly":         is_anomaly,
        # v3 §9: recommended queue count (B(2) 多队列 LRU 实施提示)
        # Count chains with cov >= 10% — these are 维护成本划算 的 chain。
        "recommended_queue_count": sum(
            1 for c in chains if (c.get("coverage_pct") or 0) >= 10.0
        ),
        "_note": "see docs/step3_algorithm_decision_matrix.md §9.2 for v2 rules; "
                 "see docs/user_report_html_redesign.md §9 for queue count rule",
    }


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
    encoder=None,  # Step 1.6: PromptEncoder; None → byte default (back-compat)
) -> tuple[dict, dict, TrieNode]:
    """One pass over the user's records, then chain forest + decode.

    Returns (user_report, chain_forest, trie_root).
    """
    if encoder is None:
        # Lazy fallback so callers from older code paths still work (e.g. tests).
        from lib.prompt_encoder import ByteLevelEncoder
        encoder = ByteLevelEncoder(block_size_bytes=args.block_size)

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

    # v3 §6: reuse_time tracking (block 维度复用间隔)
    last_seen_ts: dict[bytes, int] = {}
    reuse_times: list[int] = []

    # v3 §4: user-level traffic_spike bucket (5min window by default)
    user_req_per_window: dict[int, int] = defaultdict(int)
    spike_bucket_secs = args.spike_window_min * 60

    for rid, ts, prompt in records:
        prompts_by_rid[rid] = prompt
        req_per_min[ts // 60] += 1
        user_req_per_window[ts // spike_bucket_secs] += 1
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

        # Step 1.6: delegate Layer 2-4 to encoder.
        # byte encoder is equivalent to split_blocks + compute_prefix_path_keys.
        keys = encoder.encode(prompt)
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

        # v3 §6: reuse_time — for every key seen before, record gap since last_seen_ts.
        # Done before marking new blocks (so a fresh block doesn't generate a 0 gap).
        for k in keys:
            prev_seen = last_seen_ts.get(k)
            if prev_seen is not None:
                reuse_times.append(ts - prev_seen)
            last_seen_ts[k] = ts

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

    # New-block/s quantiles — over FULL trace (padding zero seconds).
    # Without padding, sparse traces (e.g., 15 active seconds out of 335)
    # inflate p50/p95 because we'd be quantiling over non-zero seconds only.
    # v2 fix (2026-05-14): use trace span to pad.
    new_per_sec_vals = sorted(new_per_sec.values())
    if earliest_ts is not None and latest_ts is not None and latest_ts >= earliest_ts:
        total_seconds = max(latest_ts - earliest_ts + 1, len(new_per_sec_vals))
    else:
        total_seconds = len(new_per_sec_vals)
    n_zeros = max(0, total_seconds - len(new_per_sec_vals))

    def _padded_quant(pct: float) -> int:
        if total_seconds == 0:
            return 0
        idx = (total_seconds - 1) * pct / 100.0
        lo = int(idx)
        if lo < n_zeros:
            return 0
        return new_per_sec_vals[lo - n_zeros] if new_per_sec_vals else 0

    new_q = {
        "p50": _padded_quant(50),
        "p95": _padded_quant(95),
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

    # LCP histogram (v3: tuple now (hist, quantiles, top10))
    histogram, lcp_quantiles, lcp_top10 = lcp_histogram(per_request_lcp)

    # v3 §6: reuse_time quantile + CDF points
    reuse_time_q = _reuse_time_quantiles(reuse_times)
    reuse_time_cdf = _reuse_time_cdf_log(reuse_times, n_points=50)

    # v3 §4: user-level traffic_spikes (复用 detect_traffic_spikes)
    user_traffic_spikes = detect_traffic_spikes(
        user_req_per_window,
        threshold_multiplier=args.spike_threshold,
        window_minutes=args.spike_window_min,
    )

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

    # v2 fields: avg_blocks_per_request / rpm_avg / unique_rpm_avg
    avg_blocks_per_request = total_blocks / n_total if n_total else 0.0
    trace_duration_seconds = (
        (latest_ts or 0) - (earliest_ts or 0) if (earliest_ts and latest_ts) else 0
    )
    trace_duration_minutes = trace_duration_seconds / 60.0 if trace_duration_seconds else 0.0
    rpm_avg = n_total / trace_duration_minutes if trace_duration_minutes else 0.0
    unique_rpm_avg = len(seen_keys) / trace_duration_minutes if trace_duration_minutes else 0.0

    # Top-chain summary for user_summary (+ v2 chain_length_ratio)
    dom_len = forest["chains"][0]["chain_length"] if forest["chains"] else 0
    chain_length_ratio = (
        dom_len / avg_blocks_per_request if avg_blocks_per_request else 0.0
    )
    chain_forest_summary = {
        "total_chains":    len(forest["chains"]),
        "dominant_chain_coverage_pct": (
            forest["chains"][0]["coverage_pct"] if forest["chains"] else 0.0
        ),
        "dominant_chain_length": dom_len,
        "chain_length_ratio": round(chain_length_ratio, 4),
    }

    user_report = {
        "user_id": user_id,
        "encoder_meta": {  # Step 1.6: token-vs-byte 区分; HTML banner 读这里
            "name": encoder.name,
            "block_size": args.block_size,
            "block_unit": "tokens" if encoder.name == "glm5_token_v1" else "bytes",
            "hash_algo": "sha256_chain_fallback" if encoder.name == "glm5_token_v1" else "sha256_chain",
            "chat_mode": getattr(encoder, "chat_mode", None),
            "tokenizer_path": getattr(encoder, "tokenizer_path", None),
        },
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
            "trace_duration_seconds": trace_duration_seconds,
            "avg_blocks_per_request": round(avg_blocks_per_request, 4),
            "rpm_avg":          round(rpm_avg, 4),
            "unique_rpm_avg":   round(unique_rpm_avg, 4),
            # share_of_model_unique filled in by main() after pass 2
            "share_of_model_unique": None,
            "analyze_seconds":    round(time.time() - t0, 3),
        },
        "inter_arrival_gaps_seconds":   gap_q,
        "new_unique_blocks_per_sec_q":  new_q,
        "lcp_distribution": {
            "quantiles":         lcp_quantiles,    # v3: now has p30 + p80
            "histogram":         histogram,
            "top10_lcp_values":  lcp_top10,        # v3 §7
        },
        # v3 §6 reuse_time CDF
        "reuse_time_quantiles":  reuse_time_q,
        "reuse_time_cdf_points": reuse_time_cdf,
        # v3 §4 user-level traffic spikes (model-level is in model_report.json)
        "user_traffic_spikes": user_traffic_spikes,
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

    # Step 3 recommendation is computed in a 2nd pass by main(), after
    # all selected users are analyzed — it needs model-level context
    # (reuse inversion, multi-tenant signals) that single-user analysis
    # cannot produce.
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
        rec = r.get("step3_recommendation") or {}
        cls = r.get("classifications") or {}
        rows.append({
            # ----- legacy fields (kept for backward-compat) -----
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
            "rec_primary":                rec.get("primary_algorithm", ""),
            "rec_companion":              rec.get("companion_algorithm", "") or "",
            "rec_difficulty":             rec.get("difficulty", ""),
            # ----- v2 fields (§9.2) -----
            "chain_length_ratio":         cs.get("chain_length_ratio", ""),
            "share_of_model_unique":      s.get("share_of_model_unique", ""),
            "rpm_avg":                    s.get("rpm_avg", ""),
            "unique_rpm_avg":             s.get("unique_rpm_avg", ""),
            "hit_band":                   cls.get("hit_band", ""),
            "cov_band":                   cls.get("cov_band", ""),
            "chain_len_band":             cls.get("chain_len_band", ""),
            "unique_share_band":          cls.get("unique_share_band", ""),
            "chain_count_band":           cls.get("chain_count_band", ""),
            "is_anomaly":                 cls.get("is_anomaly", ""),
            "a_subtype":                  rec.get("a_subtype", "") or "",
            "b_subtype":                  rec.get("b_subtype", "") or "",
            "c_subtype":                  rec.get("c_subtype", "") or "",
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
        # legacy
        "user_id", "request_count", "request_pct", "ideal_hit_rate",
        "chain_forest_count", "dominant_chain_cov_pct", "dominant_chain_length",
        "p50_gap", "p95_gap", "new_block_per_sec_p95",
        "rec_primary", "rec_companion", "rec_difficulty",
        # v2
        "chain_length_ratio", "share_of_model_unique",
        "rpm_avg", "unique_rpm_avg",
        "hit_band", "cov_band", "chain_len_band",
        "unique_share_band", "chain_count_band", "is_anomaly",
        "a_subtype", "b_subtype", "c_subtype",
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
    p.add_argument("--block-size",      type=int,   default=128,
                   help="bytes (byte encoder) or tokens (glm5_token encoder) per block")
    # Step 1.6: encoder strategy
    p.add_argument("--encoder", type=str, default="byte",
                   choices=["byte", "glm5_token"],
                   help="prompt encoding strategy (default: byte)")
    p.add_argument("--tokenizer-path", type=str, default="models/glm5_tokenizer",
                   help="GLM-5 tokenizer path (only used when --encoder=glm5_token)")
    p.add_argument("--chat-mode", type=str, default="wrap_user",
                   choices=["raw", "wrap_user", "messages"],
                   help="chat template wrapping (only used when --encoder=glm5_token)")
    p.add_argument("--mc-branch-threshold",   type=float, default=DEFAULT_MC_BRANCH_THRESHOLD)
    p.add_argument("--mc-coverage-threshold", type=float, default=DEFAULT_MC_COVERAGE_THRESHOLD)
    p.add_argument("--mc-min-chain-length",   type=int,   default=DEFAULT_MIN_CHAIN_LENGTH)
    p.add_argument("--mc-min-chain-coverage", type=float, default=DEFAULT_MIN_CHAIN_COVERAGE)
    p.add_argument("--mc-max-chains",         type=int,   default=DEFAULT_MAX_CHAINS)
    # v2 classification thresholds + spike config (§9.2.2)
    p.add_argument("--spike-window-min",  type=int,   default=DEFAULT_SPIKE_WINDOW_MIN,
                   help="Traffic spike detection window size in minutes (default: 5)")
    p.add_argument("--spike-threshold",   type=float, default=DEFAULT_SPIKE_THRESHOLD,
                   help="Spike trigger multiplier: bucket[i] / bucket[i-1] (default: 5.0)")
    p.add_argument("--hit-low",   type=float, default=DEFAULT_HIT_LOW,
                   help="hit_band low threshold (default 0.30)")
    p.add_argument("--hit-high",  type=float, default=DEFAULT_HIT_HIGH,
                   help="hit_band high threshold (default 0.60)")
    p.add_argument("--cov-low",   type=float, default=DEFAULT_COV_LOW,
                   help="cov_band low threshold as fraction (default 0.10)")
    p.add_argument("--cov-high",  type=float, default=DEFAULT_COV_HIGH,
                   help="cov_band high threshold as fraction (default 0.50)")
    p.add_argument("--chain-len-ratio-long", type=float, default=DEFAULT_CHAIN_LEN_RATIO_LONG,
                   help="chain_len_band long threshold (default 0.30)")
    p.add_argument("--unique-share-low",  type=float, default=DEFAULT_UNIQUE_SHARE_LOW,
                   help="unique_share_band low threshold (default 0.05)")
    p.add_argument("--unique-share-high", type=float, default=DEFAULT_UNIQUE_SHARE_HIGH,
                   help="unique_share_band high threshold (default 0.30)")
    p.add_argument("--chain-count-many",  type=int,   default=DEFAULT_CHAIN_COUNT_MANY,
                   help="chain_count_band many threshold (default 3)")
    # v3 §8: chain shadow detection thresholds
    p.add_argument("--shadow-min-shared", type=int,   default=5,
                   help="chain_shadow_pairs: min shared prefix blocks (default 5)")
    p.add_argument("--shadow-min-ratio",  type=float, default=0.30,
                   help="chain_shadow_pairs: min ratio of either chain (default 0.30)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    csv_files = discover_csv_files(args.raw_csv)
    if not csv_files:
        print(f"error: no CSV files at {args.raw_csv}", file=sys.stderr)
        sys.exit(1)
    print(f"Input: {len(csv_files)} CSV file(s) under {args.raw_csv}", flush=True)

    # Step 1.6: build encoder (byte = current baseline, glm5_token = 精确化)
    encoder, encoder_meta = build_encoder_from_args(args)
    print(f"Encoder: {encoder_meta['name']} (block_size={args.block_size} {encoder_meta['block_unit']}, "
          f"chat_mode={encoder_meta['chat_mode']})", flush=True)

    # Pass 1: counts + req_per_5min for spike detection + model-level time-series
    t_pass1 = time.time()
    (counts, total, req_per_window, model_req_per_min,
     ts_parse_failed, earliest_ts_all, latest_ts_all) = collect_user_counts(
        csv_files, window_minutes=args.spike_window_min,
    )
    spikes = detect_traffic_spikes(
        req_per_window,
        threshold_multiplier=args.spike_threshold,
        window_minutes=args.spike_window_min,
    )
    print(
        f"Pass 1: {total:,} requests across {len(counts)} users; "
        f"{len(spikes)} traffic spike(s) detected at ≥ {args.spike_threshold}× over "
        f"{args.spike_window_min}min windows  "
        f"({time.time()-t_pass1:.1f}s)",
        flush=True,
    )
    if ts_parse_failed > 0:
        print(
            f"  ⚠️  {ts_parse_failed:,} row(s) had unparseable timestamps "
            f"(fallback to 0). rpm/unique_rpm 可能偏低。",
            flush=True,
        )

    # v3 §4 / §1: 模型层 req_per_min quantile (padded to full trace minutes)
    n_users_total = len(counts)   # 全模型 user 数 (不只 Top-K)

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
    user_forests: dict[str, dict] = {}    # kept in memory for pass-2 recommendation

    # ---- Pass 1: analyze each user, write chain_forest.json now ----
    for uid in selected:
        print(f"\nAnalyzing {uid} ({counts[uid]:,} requests)...", flush=True)
        report, forest, _root = analyze_user(uid, records_by_user[uid], args, encoder=encoder)

        udir = args.output_dir / safe_dirname(uid)
        udir.mkdir(parents=True, exist_ok=True)

        with open(udir / "chain_forest.json", "w", encoding="utf-8") as f:
            json.dump(forest, f, indent=2, ensure_ascii=False)

        user_reports[uid] = report
        user_forests[uid] = forest

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

    # ---- Pass 2: model-level metrics, share_of_model_unique back-fill,
    #              classifications, then per-user Step 3 recommendation ----
    spike_config = {
        "window_minutes":       args.spike_window_min,
        "threshold_multiplier": args.spike_threshold,
    }

    # v3 §1: model-level quantile (req/min + new_block/s). 含 0 桶 padding.
    # req/min uses ALL users (model_req_per_min from pass-1, accurate).
    # new_block/s sums Top-K user new_per_sec_series (近似, long-tail user 不计).
    def _padded_quantile(values: list[int], total_buckets: int) -> dict:
        """Quantile over values list, padded with zeros to total_buckets."""
        sorted_vals = sorted(values)
        denom = max(total_buckets, len(sorted_vals)) or 1
        n_zeros = max(0, denom - len(sorted_vals))
        sum_v = sum(sorted_vals)

        def _q(pct: float) -> int:
            idx = (denom - 1) * pct / 100.0
            lo = int(idx)
            if lo < n_zeros:
                return 0
            return sorted_vals[lo - n_zeros] if sorted_vals else 0

        return {
            "avg": round(sum_v / denom, 4) if denom else 0.0,
            "p50": _q(50), "p80": _q(80), "p95": _q(95),
            "max": sorted_vals[-1] if sorted_vals else 0,
        }

    # Model-level trace span buckets (含 0)
    if latest_ts_all >= earliest_ts_all and latest_ts_all > 0:
        model_total_minutes = (latest_ts_all // 60) - (earliest_ts_all // 60) + 1
        model_total_seconds = latest_ts_all - earliest_ts_all + 1
    else:
        model_total_minutes = 1
        model_total_seconds = 1

    # req/min quantile: pass-1 model_req_per_min covers all users
    model_req_per_min_q = _padded_quantile(
        list(model_req_per_min.values()), model_total_minutes,
    )

    # new_block/s quantile: sum across Top-K selected users (per-second)
    model_new_per_sec_combined: dict[int, int] = defaultdict(int)
    for uid in selected:
        for entry in user_reports[uid]["time_series"]["new_unique_blocks_per_second"]:
            model_new_per_sec_combined[entry["second"]] += entry["count"]
    model_new_per_sec_q = _padded_quantile(
        list(model_new_per_sec_combined.values()), model_total_seconds,
    )

    model_context = compute_model_context(
        user_reports, traffic_spikes=spikes, spike_config=spike_config,
        n_users_total=n_users_total,
        ts_parse_failed_count=ts_parse_failed,
        requests_per_min_q=model_req_per_min_q,
        new_unique_blocks_per_sec_q=model_new_per_sec_q,
    )

    # Back-fill share_of_model_unique into each user's stats
    model_total_unique = model_context["total_unique_blocks_topk"]
    for uid in selected:
        u_unique = user_reports[uid]["stats"]["unique_blocks"]
        share = u_unique / model_total_unique if model_total_unique else 0.0
        user_reports[uid]["stats"]["share_of_model_unique"] = round(share, 4)

    print(
        f"\nModel context: n_users={model_context['n_users']}, "
        f"multi_tenant={model_context['is_multi_tenant']}, "
        f"hit_rate range = [{model_context['min_hit_rate']:.3f}, "
        f"{model_context['max_hit_rate']:.3f}]  "
        f"(reuse inversion ratio = {model_context['reuse_inversion_ratio']}, "
        f"triggered = {model_context['reuse_inversion']})\n"
        f"  ideal_hit_rate_aggregate = {model_context['ideal_hit_rate_aggregate']:.4f}, "
        f"rpm_avg = {model_context['rpm_avg']:.1f}, "
        f"unique_rpm_avg = {model_context['unique_rpm_avg']:.1f}",
        flush=True,
    )

    thresholds = {
        "hit_low":               args.hit_low,
        "hit_high":              args.hit_high,
        "cov_low":               args.cov_low,
        "cov_high":              args.cov_high,
        "chain_len_ratio_long":  args.chain_len_ratio_long,
        "unique_share_low":      args.unique_share_low,
        "unique_share_high":     args.unique_share_high,
        "chain_count_many":      args.chain_count_many,
    }

    for uid in selected:
        report = user_reports[uid]
        forest = user_forests[uid]
        # Compute v2 classifications (uses share_of_model_unique just filled)
        classifications = compute_classifications(
            report["stats"], report["chain_forest_summary"], thresholds,
        )
        report["classifications"] = classifications
        # v3 §8: chain_shadow_pairs (chain 间前缀重合检测, 提示 chain 数虚高)
        report["chain_shadow_pairs"] = _compute_chain_shadow_pairs(
            forest["chains"],
            min_shared=args.shadow_min_shared,
            min_ratio=args.shadow_min_ratio,
        )
        report["step3_recommendation"] = compute_step3_recommendation(
            report["stats"],
            forest["chains"],
            report["inter_arrival_gaps_seconds"],
            report["new_unique_blocks_per_sec_q"],
            model_context=model_context,
            classifications=classifications,
        )
        udir = args.output_dir / safe_dirname(uid)
        with open(udir / "user_report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    # Write model_report.json (cross-user metrics + spike events + manual placeholders)
    # Preserve manually-filled fields from a prior run so re-running the
    # analyzer doesn't clobber what the user typed in.
    model_report = dict(model_context)
    model_report["encoder_meta"] = encoder_meta  # Step 1.6
    model_report["thresholds"] = thresholds
    model_report["_note"] = (
        "Fill model_params_class / instance_count / cache_capacity_blocks manually "
        "(see decision_matrix.md §9.2.1). HTML §0 renders red when missing. "
        "Re-running the analyzer preserves these three fields."
    )
    mr_path = args.output_dir / "model_report.json"
    if mr_path.exists():
        try:
            existing = json.load(open(mr_path, encoding="utf-8"))
            for field in ("model_params_class", "instance_count",
                          "cache_capacity_blocks"):
                ev = existing.get(field)
                if ev is not None:
                    model_report[field] = ev
                    print(f"  preserved manual field {field}={ev} from existing "
                          f"model_report.json", flush=True)
        except Exception as e:
            print(f"  warning: failed to read existing model_report.json: {e}",
                  flush=True)
    with open(mr_path, "w", encoding="utf-8") as f:
        json.dump(model_report, f, indent=2, ensure_ascii=False)

    write_summary(args.output_dir, selected, excluded, user_reports, total, len(counts))
    print(
        f"\n  output → {args.output_dir}/user_summary.{{json,csv}} "
        f"+ model_report.json",
        flush=True,
    )


if __name__ == "__main__":
    main()
