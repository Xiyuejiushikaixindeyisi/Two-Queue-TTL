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


def compute_model_context(user_reports: dict[str, dict]) -> dict:
    """Per-model cross-user signals for the recommendation rules.

    Used by compute_step3_recommendation to detect reuse inversion and
    multi-tenant cache pressure scenarios that any single user's data
    cannot reveal.
    """
    hit_rates = []
    request_pcts = []
    for r in user_reports.values():
        h = (r.get("stats") or {}).get("ideal_hit_rate", 0.0)
        hit_rates.append(h)
    if not hit_rates:
        return {
            "n_users": 0, "is_multi_tenant": False,
            "max_hit_rate": 0.0, "min_hit_rate": 0.0,
            "reuse_inversion_ratio": 1.0, "reuse_inversion": False,
        }
    max_h = max(hit_rates)
    min_h = min(hit_rates)
    # Inversion definition: max ≥ 2× min, where min == 0 (a user with no
    # cache reuse at all) is treated as an extreme inversion case (ratio
    # reported as a large sentinel value, inversion = True if max > 0).
    if min_h > 0:
        ratio = max_h / min_h
        inversion = ratio >= 2.0
    elif max_h > 0:
        # One user has hit_rate 0 while another has >0 → extreme inversion
        ratio = float("inf")
        inversion = True
    else:
        ratio = 1.0
        inversion = False
    n_users = len(user_reports)
    return {
        "n_users": n_users,
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
    }


def compute_step3_recommendation(
    report_stats: dict, chains: list[dict], inter_arrival: dict,
    new_per_sec_q: dict, model_context: dict | None = None,
) -> dict:
    """Decide primary + companion algorithm per decision matrix §3 rules.

    Inputs are subsets of the user_report fields. Output is a dict written
    to user_report.json as a top-level field, and rendered as §6 in HTML.

    `model_context` carries cross-user signals (reuse inversion, multi-tenant
    flag, hit-rate distribution); when None, the function falls back to
    single-user mode (no A-primary inversion path available).

    See docs/step3_algorithm_decision_matrix.md for the underlying rules.
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
    is_multi = ctx.get("is_multi_tenant", False)
    has_inversion = ctx.get("reuse_inversion", False)
    inversion_ratio = ctx.get("reuse_inversion_ratio")
    n_users_ctx = ctx.get("n_users", 1)

    # Decision rules per matrix §3 (revised 2026-05-12 to recognize A's
    # core value in reuse-inversion / multi-tenant cache isolation).
    primary, companion = None, None

    pin_uplift_pp = (top_cov_pct / 100.0) * (1.0 - hit_rate) * 100.0
    business_ceiling_low = hit_rate < 0.25 and pin_uplift_pp < 5.0

    if is_multi and has_inversion:
        # A-priority (highest in multi-tenant reuse-inversion).
        # Per-user cache partition prevents low-reuse users from evicting
        # the high-reuse users' chains — even if this user's own hit rate
        # is poor (D would apply otherwise), routing isolation still
        # protects the rest of the tenants.
        primary = "A"
        if business_ceiling_low:
            # Low-hit user in inversion scenario: route to isolate, then
            # bring in business-side rewrite as a secondary lever.
            companion = "D"
        elif n_chains > 0:
            companion = "B"
        elif unique >= 200_000:
            companion = "C"
    elif business_ceiling_low:
        # D-priority (single-tenant or non-inversion multi-tenant low hit)
        primary = "D"
        if n_chains > 0:
            companion = "B"   # light pin still rescues a tiny bit
    elif unique >= 1_000_000 and hit_rate >= 0.7:
        # C-priority: extreme cache pressure + already-high reuse
        primary = "C"
        if top_len >= 100:
            companion = "B"
        elif is_multi:
            companion = "A"
    elif is_multi and (unique >= 200_000 or new_p95 >= 200):
        # A-priority case 2: multi-tenant + moderate cache pressure.
        # Even without reuse inversion, cross-user driver in a shared
        # cache hurts everyone; per-user routing + capacity helps.
        primary = "A"
        if unique >= 200_000:
            companion = "C"
        elif n_chains > 0:
            companion = "B"
    elif n_chains > 0:
        # B default: chain dominates the user's business
        primary = "B"
        if is_multi:
            companion = "A"     # multi-tenant: pair pin with routing isolation
        elif unique >= 200_000:
            companion = "C"     # single-tenant but capacity matters
    else:
        # No chain forest detected
        primary = "D"
        if is_multi:
            companion = "A"

    # Difficulty per algorithm
    difficulty_map = {
        "A": "high",       # routing layer requires infra changes
        "B": "low",        # vLLM-level chain pin is a known feature
        "C": "medium",     # capacity expansion cheap; pooling/quantization harder
        "D": "high",       # business coordination required
    }
    difficulty = difficulty_map.get(primary, "unknown")

    estimated = _estimate_uplift(primary, hit_rate, top_cov_pct, n_chains,
                                 total_reqs)

    reasons = _make_reasons(
        hit_rate, n_chains, top_cov_pct, top_len, unique, new_p95,
        business_type,
    )
    # Extra cross-user reasons when model context triggered A
    if primary == "A":
        if has_inversion:
            reasons.append(
                f"模型级复用倒置: hit_rate max/min = {inversion_ratio}x "
                f"(≥ 2.0 触发 A 路由，按 user 隔离 cache 防止驱逐)"
            )
        elif is_multi:
            reasons.append(
                f"多租户场景 ({n_users_ctx} users) + cache 压力 → 路由分区减少 "
                f"cross-user 驱逐"
            )

    # Implementation steps per primary algorithm
    impl_map = {
        "A": [
            "按 user_id 隔离 cache（multi-tenant cache partition），防止重度/低复用用户驱逐其他 user 的 chain",
            "高优先级用户路由到独立实例 / 独立 cache 池（特别是复用倒置场景的高 hit 用户）",
            "同 system_prompt 的 request 路由到同实例（prefix-aware batching）",
            "Step 2 验证：测真实 cache 容量在 user 隔离下能否容纳全部 user chain；同 batch 内 cache 命中行为",
        ],
        "B": [
            f"识别并 pin top {n_chains} 条 chain (dominant chain {top_len} block / cov {top_cov_pct:.1f}%)",
            "每条 chain 一个独立 LRU 队列 (避免不同 chain 互相驱逐)",
            "监控 chain hash 生命周期, prompt 版本漂移时及时刷新 pin",
            "如有 shadow group (人工标注), 同组 chain 合并为一个 pin unit",
        ],
        "C": [
            f"评估 cache 容量是否能 hold {unique:,} unique blocks",
            "容量不足时考虑: 物理扩容 / KV 量化 (fp8 或 int8) / 跨实例池化",
            "重要 chain 配合 B 算法做选择性 pin",
            "多租户场景下与 A 配合：先按 user 分区，再各分区内 LRU + 量化",
        ],
        "D": [
            "与业务方沟通: 减少 prompt 中的动态字段 (request_id / timestamp / 随机 seed)",
            "评估 system prompt 是否可以模板化、降低同 user 内多版本漂移",
            "如业务上限本身低 (如分类任务), 接受 prefix cache 不是主要优化点; 转向 prefill 内核优化",
        ],
    }
    implementation = impl_map.get(primary, [])

    return {
        "primary_algorithm":  primary,
        "companion_algorithm": companion,
        "business_type":      business_type,
        "business_evidence":  evidence,
        "reasons":            reasons,
        "difficulty":         difficulty,
        "estimated_uplift":   estimated,
        "implementation_steps": implementation,
        "model_context_snapshot": {
            "n_users":          n_users_ctx,
            "is_multi_tenant":  is_multi,
            "reuse_inversion":  has_inversion,
            "reuse_inversion_ratio": inversion_ratio,
        } if model_context else None,
        "_note": "see docs/step3_algorithm_decision_matrix.md for the full rule set",
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
            "rec_primary":                rec.get("primary_algorithm", ""),
            "rec_companion":              rec.get("companion_algorithm", "") or "",
            "rec_difficulty":             rec.get("difficulty", ""),
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
    user_forests: dict[str, dict] = {}    # kept in memory for pass-2 recommendation

    # ---- Pass 1: analyze each user, write chain_forest.json now ----
    for uid in selected:
        print(f"\nAnalyzing {uid} ({counts[uid]:,} requests)...", flush=True)
        report, forest, _root = analyze_user(uid, records_by_user[uid], args)

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

    # ---- Pass 2: compute model-level context, then per-user Step 3 recommendation ----
    model_context = compute_model_context(user_reports)
    print(
        f"\nModel context: n_users={model_context['n_users']}, "
        f"multi_tenant={model_context['is_multi_tenant']}, "
        f"hit_rate range = [{model_context['min_hit_rate']:.3f}, "
        f"{model_context['max_hit_rate']:.3f}]  "
        f"(reuse inversion ratio = {model_context['reuse_inversion_ratio']}, "
        f"triggered = {model_context['reuse_inversion']})",
        flush=True,
    )

    for uid in selected:
        report = user_reports[uid]
        forest = user_forests[uid]
        report["step3_recommendation"] = compute_step3_recommendation(
            report["stats"],
            forest["chains"],
            report["inter_arrival_gaps_seconds"],
            report["new_unique_blocks_per_sec_q"],
            model_context=model_context,
        )
        # Now write user_report.json (with recommendation)
        udir = args.output_dir / safe_dirname(uid)
        with open(udir / "user_report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    write_summary(args.output_dir, selected, excluded, user_reports, total, len(counts))
    print(f"\n  output → {args.output_dir}/user_summary.{{json,csv}}", flush=True)


if __name__ == "__main__":
    main()
