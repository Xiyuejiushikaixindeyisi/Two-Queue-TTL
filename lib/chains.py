"""Canonical prefix-trie + chain-forest algorithm — single source of truth.

此前散在 scripts/verify_chain_path_closure.py (TrieNode/trie_insert)、
scripts/multi_chain_finder.py (find_chain_forest + 阈值常量)、
scripts/per_user_report_4variant.py (build_trie/count_trie_hits)。主线入口 app_report
为了用它们曾 import 这些 legacy 脚本内部, 现统一到此处, 所有 importer 改指向 lib.chains。

block key 一律是 bytes (prefix-path chain hash, 见 lib/chain_key.py 与 docs/terminology.md)。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Optional

# Multi-chain default thresholds (see design doc §5.3)
DEFAULT_MC_BRANCH_THRESHOLD = 0.05
DEFAULT_MC_COVERAGE_THRESHOLD = 0.05
DEFAULT_MIN_CHAIN_LENGTH = 10
DEFAULT_MIN_CHAIN_COVERAGE = 0.01
DEFAULT_MAX_CHAINS = 50


@dataclass
class TrieNode:
    count: int = 0
    children: dict = field(default_factory=dict)
    sample_request_id: Optional[str] = None  # any request following this path


def trie_insert(root: TrieNode, keys: list[bytes], request_id: str) -> None:
    node = root
    node.count += 1
    if node.sample_request_id is None:
        node.sample_request_id = request_id
    for key in keys:
        child = node.children.get(key)
        if child is None:
            child = TrieNode()
            node.children[key] = child
        child.count += 1
        if child.sample_request_id is None:
            child.sample_request_id = request_id
        node = child


def build_trie(records: list[dict], variant: str) -> tuple[TrieNode, int, int]:
    """Build a prefix trie for one variant's hash_ids. records: [{hash_ids:{v:[bytes]}, request_id}]."""
    root = TrieNode()
    total_requests = 0
    total_blocks = 0
    for r in records:
        keys = r["hash_ids"].get(variant, [])
        if not keys:
            continue
        trie_insert(root, keys, r["request_id"])
        total_requests += 1
        total_blocks += len(keys)
    return root, total_requests, total_blocks


def count_trie_hits(node: TrieNode) -> int:
    """Sum (count - 1) over all non-root nodes — total prefix-cache hits.

    每个非根节点代表一个具体前缀位置: count 个请求访问过, 第 1 次 miss, 余 count-1 次 hit。
    """
    hits = 0
    for child in node.children.values():
        hits += child.count - 1
        hits += count_trie_hits(child)
    return hits


# ---------------------------------------------------------------------------
# Chain forest: recursion (emit leaf chains only — see §5.2 design note)
# ---------------------------------------------------------------------------

def _multi_chain_recursive(
    node: TrieNode,
    mc_branch_thr: float,
    mc_cov_thr: float,
    total_requests: int,
    path_keys: list[bytes],
    path_counts: list[int],
    first_branch_pos,
) -> list[dict]:
    """Recursive chain explorer. Only emits *leaf* chains.

    A leaf chain is a path that cannot be extended further under the
    thresholds — either because the node has no children at all (natural
    leaf) or because no child satisfies the ratio + coverage bounds
    (logical leaf). This avoids emitting nested redundant chains like
    [A] / [A, B] / [A, B, C].
    """
    eligible = []
    for k, child in node.children.items():
        cov = child.count / total_requests
        ratio = child.count / node.count
        if cov >= mc_cov_thr and ratio >= mc_branch_thr:
            eligible.append((k, child))

    if not eligible:
        if not path_keys:
            return []
        return [{
            "keys":   list(path_keys),
            "counts": list(path_counts),
            "first_branch_position": first_branch_pos,
            # leaf node's sample request — fully traverses this chain. Used to
            # decode the chain's shared-prefix content (system prompt).
            "sample_request_id": node.sample_request_id,
        }]

    next_first_branch = first_branch_pos
    if next_first_branch is None and len(eligible) > 1:
        # The current node is the first divergence point; the branches
        # diverge starting at the next step (position = len(path_keys)).
        next_first_branch = len(path_keys)

    chains: list[dict] = []
    for k, child in eligible:
        chains.extend(_multi_chain_recursive(
            child, mc_branch_thr, mc_cov_thr, total_requests,
            path_keys + [k], path_counts + [child.count],
            next_first_branch,
        ))
    return chains


# ---------------------------------------------------------------------------
# Pruning + ranking
# ---------------------------------------------------------------------------

def _apply_pruning(
    raw_chains: list[dict],
    total_requests: int,
    min_chain_length: int,
    min_chain_coverage: float,
    max_chains: int,
) -> tuple[list[dict], dict]:
    """Apply length, coverage, and top-N caps in that order."""
    after_length = [c for c in raw_chains if len(c["keys"]) >= min_chain_length]
    after_coverage = [
        c for c in after_length
        if (c["counts"][-1] / total_requests) >= min_chain_coverage
    ]
    # Sort by coverage desc, then by length desc as a stable tiebreaker
    after_coverage.sort(key=lambda c: (-c["counts"][-1], -len(c["keys"])))
    capped = after_coverage[:max_chains]

    return capped, {
        "total_chains_before_pruning":         len(raw_chains),
        "total_chains_after_length_pruning":   len(after_length),
        "total_chains_after_coverage_pruning": len(after_coverage),
        "total_chains_after_max_cap":          len(capped),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def find_chain_forest(
    root: TrieNode,
    mc_branch_thr: float = DEFAULT_MC_BRANCH_THRESHOLD,
    mc_cov_thr: float = DEFAULT_MC_COVERAGE_THRESHOLD,
    min_chain_length: int = DEFAULT_MIN_CHAIN_LENGTH,
    min_chain_coverage: float = DEFAULT_MIN_CHAIN_COVERAGE,
    max_chains: int = DEFAULT_MAX_CHAINS,
) -> dict:
    """Explore the trie + apply pruning. Returns chain_forest dict per §5.5.

    The returned dict does NOT include decoded_content — decoding requires
    raw CSV access and is the orchestrator's responsibility.
    """
    params = {
        "mc_branch_threshold":   mc_branch_thr,
        "mc_coverage_threshold": mc_cov_thr,
        "mc_min_chain_length":   min_chain_length,
        "mc_min_chain_coverage": min_chain_coverage,
        "mc_max_chains":         max_chains,
    }
    total = root.count

    if total == 0 or not root.children:
        return {
            "params": params,
            "stats": {
                "total_chains_before_pruning":         0,
                "total_chains_after_length_pruning":   0,
                "total_chains_after_coverage_pruning": 0,
                "total_chains_after_max_cap":          0,
                "trie_total_requests": total,
            },
            "chains": [],
        }

    # Deep tries can blow the default recursion limit (Qwen-64K requests
    # reach ~1700 blocks); raise it to a safe headroom.
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, 20000))
    try:
        raw = _multi_chain_recursive(
            root, mc_branch_thr, mc_cov_thr, total,
            path_keys=[], path_counts=[], first_branch_pos=None,
        )
    finally:
        sys.setrecursionlimit(old_limit)

    pruned, stage_counts = _apply_pruning(
        raw, total, min_chain_length, min_chain_coverage, max_chains,
    )

    chains_out = []
    for i, c in enumerate(pruned):
        bp = c["first_branch_position"]
        # Prefix coverage (counts on the trie are monotonically non-increasing
        # along the chain, so coverage_pcts is non-increasing too). Exposes the
        # signal hidden by leaf-only output — see portraits §3.7 / design §5.5.
        coverage_pcts = [round(cnt / total * 100, 3) for cnt in c["counts"]]
        chains_out.append({
            "chain_id":      i,
            "chain_length":  len(c["keys"]),
            "coverage_count": c["counts"][-1],
            "coverage_pct":  round(c["counts"][-1] / total * 100, 3),
            "max_prefix_coverage_pct": coverage_pcts[0],
            "coverage_pcts": coverage_pcts,
            "branch_at_root_position": bp,
            "branch_at_root_ratio":
                _branch_ratio(c["counts"], bp, total) if bp is not None else None,
            "sample_request_id": c.get("sample_request_id"),
            "keys":   [k.hex() for k in c["keys"]],
            "counts": c["counts"],
        })

    return {
        "params": params,
        "stats":  {**stage_counts, "trie_total_requests": total},
        "chains": chains_out,
    }


def _branch_ratio(counts: list[int], branch_pos: int, total_requests: int) -> float:
    """Ratio = counts[branch_pos] / parent.count at that branch."""
    if branch_pos == 0:
        return round(counts[0] / total_requests, 4)
    return round(counts[branch_pos] / counts[branch_pos - 1], 4)
