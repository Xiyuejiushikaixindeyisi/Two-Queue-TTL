"""Canonical ideal hit-rate / LCP helpers — single source of truth.

ideal (无淘汰) prefix-cache 命中口径: block key 是 chain hash → 某 key 见过则其所有
前缀 key 必见过, 故 LCP = 从 block 0 起连续命中数 (单次扫描即可)。每个 key 第一次
出现必 miss、之后必 hit、seen 只增 → 累计 hit = total_blocks - unique_blocks, 与请求
顺序无关 (reuse_time 则依赖顺序, 见 lib/reuse_time.py)。

定义忠实抽取自 per_user_report_analyzer.py (LCP + lcp_histogram + percentile_int),
供 dataset_hit_rate / model_report / app_report 共用, 避免口径漂移。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable


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


def lcp(keys: list, seen: set) -> int:
    """从 block 0 起连续命中 seen 的 block 数 (longest common prefix)。不修改 seen。"""
    n = 0
    for k in keys:
        if k in seen:
            n += 1
        else:
            break
    return n


def pooled_hit_rate(key_lists: Iterable[list]) -> dict:
    """对一串请求的 block-key 列表算整库 pooled 理想命中率。

    Returns {total_blocks, unique_blocks, hit_blocks, ideal_hit_rate}。
    """
    seen: set = set()
    hit = total = 0
    for keys in key_lists:
        if not keys:
            continue
        total += len(keys)
        hit += lcp(keys, seen)
        seen.update(keys)
    return {
        "total_blocks": total,
        "unique_blocks": len(seen),
        "hit_blocks": hit,
        "ideal_hit_rate": (hit / total) if total else 0.0,
    }


def lcp_histogram(lcps: list[int]) -> tuple[list[dict], dict, list[dict]]:
    """Equal-width histogram with ~30 buckets.

    Returns: (histogram, quantiles_dict, top10_lcp_values)
      - histogram:    [{lcp_low, lcp_high, count}] equal-width buckets
      - quantiles:    {p30, p50, p80, p95, max, bucket_size}
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
        {"lcp_low": b * bucket_size, "lcp_high": (b + 1) * bucket_size - 1, "count": n}
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
    top10 = [
        {"lcp_value": v, "request_count": c}
        for v, c in Counter(lcps).most_common(10)
    ]
    return histogram, quantiles, top10
