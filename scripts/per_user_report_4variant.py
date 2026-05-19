#!/usr/bin/env python3
"""4-variant prompt-rewrite analyzer (Stage 3).

Reads a trace CSV produced by `convert_trace.py --mode chat` containing 4
hash_ids columns (base / reorder / placeholder / both), and emits per-user
JSON with each variant's:

  - ideal_hit_rate     (prefix block hit rate, vllm-aligned)
  - chain_count        (number of significant chains after pruning)
  - chain_avg_length   (average chain length in blocks)
  - chain_coverage     (fraction of total block-visits falling on chains)
  - delta_vs_base      (each metric's delta against the base variant)

Outputs
-------
  <output-dir>/summary_4variant.json   — top-line per-user comparison
  <output-dir>/<user_id>_4variant.json — detailed per-user breakdown

This is a NEW analyzer, NOT a modification of `per_user_report_analyzer.py`.
The existing analyzer (1700+ lines, single-trace single-variant) keeps working
unchanged. Rendering goes through `render_user_report_4variant_html.py`.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verify_chain_path_closure import TrieNode, trie_insert  # noqa: E402
from multi_chain_finder import (  # noqa: E402
    DEFAULT_MAX_CHAINS,
    DEFAULT_MC_BRANCH_THRESHOLD,
    DEFAULT_MC_COVERAGE_THRESHOLD,
    DEFAULT_MIN_CHAIN_COVERAGE,
    DEFAULT_MIN_CHAIN_LENGTH,
    find_chain_forest,
)

csv.field_size_limit(10 * 1024 * 1024)

VARIANTS = ("base", "reorder", "placeholder", "both")


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def _hex_to_bytes(hex_keys: list[str]) -> list[bytes]:
    out: list[bytes] = []
    for k in hex_keys:
        k = k.strip()
        if not k:
            continue
        try:
            out.append(bytes.fromhex(k))
        except ValueError:
            # Skip malformed hash; should not happen for properly converted traces.
            continue
    return out


def load_4variant_trace(csv_path: Path) -> dict[str, list[dict]]:
    """Group records by user_id, preserving per-variant hash lists."""
    users: dict[str, list[dict]] = defaultdict(list)
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        present_variants = [v for v in VARIANTS if f"hash_ids_{v}" in (reader.fieldnames or [])]
        if not present_variants:
            raise ValueError(
                f"No hash_ids_<variant> columns found in {csv_path}; "
                f"expected at least one of {[f'hash_ids_{v}' for v in VARIANTS]}"
            )
        for i, row in enumerate(reader):
            user_id = (row.get("user_id") or "_anon").strip() or "_anon"
            hash_per_variant: dict[str, list[bytes]] = {}
            for v in present_variants:
                raw = row.get(f"hash_ids_{v}", "") or ""
                hash_per_variant[v] = _hex_to_bytes(raw.split("|"))
            users[user_id].append({
                "request_id": str(i),
                "timestamp": row.get("timestamp", ""),
                "hash_ids": hash_per_variant,
            })
    return dict(users), present_variants


# ---------------------------------------------------------------------------
# Per-variant metrics
# ---------------------------------------------------------------------------

def _build_trie(records: list[dict], variant: str) -> tuple[TrieNode, int, int]:
    """Build a prefix trie for one variant's hash_ids."""
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


def _count_trie_hits(node: TrieNode) -> int:
    """Sum (count - 1) over all non-root nodes — total prefix-cache hits.

    Reasoning: each non-root node represents a specific prefix position. If
    `count` requests visited this prefix, the first visit is a miss (cold)
    and the remaining `count - 1` are hits (warm).
    """
    hits = 0
    for child in node.children.values():
        hits += child.count - 1
        hits += _count_trie_hits(child)
    return hits


def _summarize_chain_forest(forest: dict, total_blocks: int) -> dict:
    """chain_count / chain_avg_length / chain_coverage."""
    chains = forest.get("chains", [])
    if not chains:
        return {"chain_count": 0, "chain_avg_length": 0.0, "chain_coverage": 0.0}
    lengths = [c["chain_length"] for c in chains]
    # Coverage: sum of chain_length × coverage_count / total trie blocks.
    # Monotonic in "fraction of block-visits falling on a discovered chain".
    coverage_blocks = sum(c["chain_length"] * c["coverage_count"] for c in chains)
    return {
        "chain_count": len(chains),
        "chain_avg_length": sum(lengths) / len(lengths),
        "chain_coverage": coverage_blocks / total_blocks if total_blocks else 0.0,
    }


def analyze_user_4variant(
    records: list[dict],
    variants: list[str],
    chain_kwargs: dict,
) -> dict:
    """Run analysis for all variants in one user's records."""
    out: dict[str, dict] = {}
    for v in variants:
        root, n_req, n_blocks = _build_trie(records, v)
        if n_req == 0:
            out[v] = {
                "requests": 0, "blocks": 0,
                "ideal_hit_rate": 0.0,
                "chain_count": 0,
                "chain_avg_length": 0.0,
                "chain_coverage": 0.0,
            }
            continue
        hits = _count_trie_hits(root)
        forest = find_chain_forest(root, **chain_kwargs)
        out[v] = {
            "requests": n_req,
            "blocks": n_blocks,
            "ideal_hit_rate": hits / n_blocks if n_blocks else 0.0,
            **_summarize_chain_forest(forest, n_blocks),
        }
    return out


def compute_deltas_vs_base(per_variant: dict) -> dict:
    base = per_variant.get("base", {})
    deltas: dict[str, dict] = {}
    for v, m in per_variant.items():
        if v == "base":
            continue
        deltas[v] = {
            "ideal_hit_rate_delta":   m["ideal_hit_rate"]   - base.get("ideal_hit_rate", 0.0),
            "chain_count_delta":      m["chain_count"]      - base.get("chain_count", 0),
            "chain_avg_length_delta": m["chain_avg_length"] - base.get("chain_avg_length", 0.0),
            "chain_coverage_delta":   m["chain_coverage"]   - base.get("chain_coverage", 0.0),
        }
    return deltas


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="4-variant prompt-rewrite analyzer (Stage 3 entry)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--trace", required=True,
                   help="4-variant CSV from convert_trace.py --mode chat")
    p.add_argument("--output-dir", required=True,
                   help="Directory to write per-user JSON + summary")
    p.add_argument("--top-k-users", type=int, default=3,
                   help="Analyze the top-K users by request count")
    p.add_argument("--min-requests", type=int, default=2,
                   help="Skip users with fewer than this many requests")
    p.add_argument("--mc-branch-thr",   type=float, default=DEFAULT_MC_BRANCH_THRESHOLD)
    p.add_argument("--mc-cov-thr",      type=float, default=DEFAULT_MC_COVERAGE_THRESHOLD)
    p.add_argument("--mc-min-len",      type=int,   default=DEFAULT_MIN_CHAIN_LENGTH)
    p.add_argument("--mc-min-cov",      type=float, default=DEFAULT_MIN_CHAIN_COVERAGE)
    p.add_argument("--mc-max-chains",   type=int,   default=DEFAULT_MAX_CHAINS)
    return p.parse_args()


def main() -> None:
    args = parse_args()

    print(f"Loading 4-variant trace: {args.trace}")
    users, variants = load_4variant_trace(Path(args.trace))
    print(f"  {len(users)} users, variants present: {variants}")

    chain_kwargs = {
        "mc_branch_thr":      args.mc_branch_thr,
        "mc_cov_thr":         args.mc_cov_thr,
        "min_chain_length":   args.mc_min_len,
        "min_chain_coverage": args.mc_min_cov,
        "max_chains":         args.mc_max_chains,
    }

    selected = sorted(
        ((uid, recs) for uid, recs in users.items() if len(recs) >= args.min_requests),
        key=lambda kv: -len(kv[1]),
    )[:args.top_k_users]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary: list[dict] = []
    for user_id, records in selected:
        print(f"  Analyzing user {user_id} ({len(records)} requests)...")
        per_variant = analyze_user_4variant(records, variants, chain_kwargs)
        deltas = compute_deltas_vs_base(per_variant)

        user_detail = {
            "user_id": user_id,
            "request_count": len(records),
            "variants_analyzed": variants,
            "variants": per_variant,
            "deltas_vs_base": deltas,
        }
        out_path = output_dir / f"{_safe(user_id)}_4variant.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(user_detail, f, ensure_ascii=False, indent=2)

        row = {
            "user_id": user_id,
            "request_count": len(records),
        }
        for v in variants:
            row[f"hit_rate_{v}"] = per_variant[v]["ideal_hit_rate"]
            row[f"chain_count_{v}"] = per_variant[v]["chain_count"]
        summary.append(row)

    with open(output_dir / "summary_4variant.json", "w", encoding="utf-8") as f:
        json.dump({
            "trace": str(Path(args.trace).resolve()),
            "variants": variants,
            "users": summary,
        }, f, ensure_ascii=False, indent=2)

    print(f"Wrote {len(summary)} per-user files + summary_4variant.json → {output_dir}")


def _safe(user_id: str) -> str:
    """Make user_id safe for use as a filename."""
    keep = "-_.@"
    return "".join(c if c.isalnum() or c in keep else "_" for c in user_id) or "_anon"


if __name__ == "__main__":
    main()
