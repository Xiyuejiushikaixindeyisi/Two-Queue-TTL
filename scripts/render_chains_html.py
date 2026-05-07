#!/usr/bin/env python3
"""Render per_user_chains*.json to a self-contained static HTML report.

Sections in the HTML:
  1. Params           (branch_threshold, coverage_threshold, block_size)
  2. Stats            (totals)
  3. User Aggregate   (chain coverage stats across users)
  4. Global Chain     (numeric metrics + concatenated decoded text)
  5. Per-User Chains  (one block per user; heavy users highlighted)

Decoded text per block is concatenated into a single readable system prompt
view per chain (no per-block clutter). Heavy users (request share >=
--heavy-pct, default 0.20) are visually emphasized.

Pure stdlib; offline-safe.

Usage
-----
  python scripts/render_chains_html.py \
      --input  outputs/dsk8k_2h_5k/per_user_chains_45.json \
      --output outputs/dsk8k_2h_5k/per_user_chains_45.html
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# CSS — keep self-contained so HTML is one file
# ---------------------------------------------------------------------------

CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  max-width: 1180px; margin: 24px auto; padding: 0 24px; color: #2d3748;
  line-height: 1.5; }
h1 { border-bottom: 2px solid #2d3748; padding-bottom: 8px; }
h2 { color: #2b6cb0; margin-top: 36px; padding-bottom: 4px;
  border-bottom: 1px solid #cbd5e0; }
.meta { background: #edf2f7; padding: 10px 16px; border-left: 4px solid #4299e1;
  font-family: 'SF Mono', Consolas, Menlo, monospace; font-size: 0.85em;
  word-break: break-all; }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 10px; margin-top: 12px; }
.stat-item { background: #f7fafc; padding: 10px 14px; border-radius: 4px;
  border-left: 3px solid #cbd5e0; }
.stat-key { color: #718096; font-size: 0.8em; text-transform: uppercase;
  letter-spacing: 0.05em; }
.stat-val { font-size: 1.25em; font-weight: 600; color: #2d3748;
  font-variant-numeric: tabular-nums; word-break: break-all; }
.user-block { border: 1px solid #e2e8f0; border-radius: 6px;
  padding: 14px 20px; margin-bottom: 14px; background: #fff; }
.user-block.heavy { border-left: 5px solid #e53e3e; background: #fffaf0; }
.user-block.global { border-left: 5px solid #2d3748; background: #f7fafc; }
.user-block.no-chain { border-left: 5px solid #a0aec0; background: #f9f9f9; }
.user-header { display: flex; justify-content: space-between; align-items: baseline;
  flex-wrap: wrap; gap: 12px; margin-bottom: 6px; }
.user-id { font-family: 'SF Mono', Consolas, monospace; font-size: 1.05em;
  font-weight: 600; color: #2d3748; word-break: break-all; }
.user-meta { display: flex; gap: 16px; font-size: 0.88em; color: #4a5568;
  flex-wrap: wrap; }
.user-meta b { color: #2d3748; }
.badge { display: inline-block; padding: 2px 9px; border-radius: 12px;
  font-size: 0.78em; font-weight: 500; vertical-align: middle; }
.badge-heavy { background: #e53e3e; color: white; }
.badge-no-chain { background: #a0aec0; color: white; }
.badge-match { background: #38a169; color: white; }
.badge-unique { background: #805ad5; color: white; }
.prompt { background: #1a202c; color: #e2e8f0; padding: 16px;
  border-radius: 4px; font-family: 'SF Mono', Consolas, Menlo, monospace;
  font-size: 0.83em; white-space: pre-wrap; word-break: break-all;
  max-height: 500px; overflow-y: auto; margin-top: 10px; line-height: 1.4; }
.prompt-empty { font-style: italic; color: #a0aec0; padding: 8px; }
details > summary { cursor: pointer; font-weight: 500; color: #2b6cb0;
  user-select: none; padding: 4px 0; }
details[open] > summary { margin-bottom: 6px; }
.no-chain-msg { font-style: italic; color: #718096; padding: 8px 0; }
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_int(n) -> str:
    if n is None:
        return "—"
    return f"{n:,}"


def fmt_pct(n) -> str:
    if n is None:
        return "—"
    return f"{n:.2f}%"


def fmt_num(n) -> str:
    if n is None:
        return "—"
    if isinstance(n, float):
        return f"{n:.4f}".rstrip("0").rstrip(".") or "0"
    return str(n)


def stat_grid(items: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<div class="stat-item"><div class="stat-key">{html.escape(k)}</div>'
        f'<div class="stat-val">{html.escape(v)}</div></div>'
        for k, v in items
    )
    return f'<div class="stat-grid">{cells}</div>'


def concat_decoded(lcp_content: list[dict]) -> str:
    """Join all decoded_text blocks into a single string.

    Skips blocks where decoded_text is None.
    """
    parts: list[str] = []
    for b in lcp_content:
        text = b.get("decoded_text")
        if text is not None:
            parts.append(text)
    return "".join(parts)


def prompt_block(text: str, n_blocks: int, n_decoded: int) -> str:
    if not text and n_blocks == 0:
        return '<div class="no-chain-msg">(no chain)</div>'
    if not text:
        return f'<div class="prompt-empty">(chain has {n_blocks} blocks but no decoded text available)</div>'
    note = (
        f" — concatenated from {n_decoded}/{n_blocks} decoded blocks"
        if n_decoded < n_blocks else ""
    )
    return (
        f'<details open><summary>System Prompt{html.escape(note)}</summary>'
        f'<pre class="prompt">{html.escape(text)}</pre></details>'
    )


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def section_params(data: dict) -> str:
    p = data.get("params", {})
    items = [
        ("branch_threshold", fmt_num(p.get("branch_threshold"))),
        ("coverage_threshold", fmt_num(p.get("coverage_threshold"))),
        ("block_size", fmt_int(p.get("block_size"))),
    ]
    return f"<h2>1. Params</h2>{stat_grid(items)}"


def section_stats(data: dict) -> str:
    s = data.get("stats", {})
    items = [
        ("total_requests", fmt_int(s.get("total_requests"))),
        ("total_users", fmt_int(s.get("total_users"))),
        ("total_blocks", fmt_int(s.get("total_blocks"))),
        ("empty_prompts", fmt_int(s.get("empty_prompts"))),
        ("build_seconds", fmt_num(s.get("build_seconds"))),
    ]
    return f"<h2>2. Stats</h2>{stat_grid(items)}"


def section_user_aggregate(data: dict) -> str:
    a = data.get("user_aggregate", {})
    items = [
        ("users_with_chain", fmt_int(a.get("users_with_chain"))),
        ("users_with_no_chain", fmt_int(a.get("users_with_no_chain"))),
        ("matching_global_fully", fmt_int(a.get("users_matching_global_fully"))),
        ("matching_global_50pct_prefix", fmt_int(a.get("users_matching_global_50pct_prefix"))),
        ("users_with_unique_chain", fmt_int(a.get("users_with_unique_chain"))),
    ]
    return f"<h2>3. User Aggregate</h2>{stat_grid(items)}"


def section_global_chain(data: dict) -> str:
    g = data.get("global_chain", {})
    chain_length = g.get("chain_length", 0)
    coverage_count = g.get("chain_coverage_count", 0)
    coverage_pct = g.get("chain_coverage_pct", 0)
    lcp_content = g.get("lcp_content", []) or []

    metrics = stat_grid([
        ("chain_length", fmt_int(chain_length)),
        ("chain_coverage_count", fmt_int(coverage_count)),
        ("chain_coverage_pct", fmt_pct(coverage_pct)),
    ])

    decoded_text = concat_decoded(lcp_content)
    n_decoded = sum(1 for b in lcp_content if b.get("decoded_text") is not None)
    prompt = prompt_block(decoded_text, chain_length, n_decoded)

    return f"""<h2>4. Global Chain</h2>
<div class="user-block global">
  <div class="user-header">
    <div class="user-id">[GLOBAL]</div>
    <div class="user-meta">
      <span><b>sample_request_id:</b> {html.escape(str(g.get("sample_request_id") or "—"))}</span>
    </div>
  </div>
  {metrics}
  {prompt}
</div>"""


def section_per_user(data: dict, heavy_pct: float) -> str:
    users = data.get("users", []) or []
    total_requests = data.get("stats", {}).get("total_requests", 0) or 0

    parts = ["<h2>5. Per-User Chains</h2>"]
    for u in users:
        uid = u.get("user_id", "")
        reqs = u.get("request_count", 0)
        share = (reqs / total_requests) if total_requests else 0.0
        chain_len = u.get("chain_length", 0)
        cov_count = u.get("chain_coverage_count", 0)
        cov_pct = u.get("chain_coverage_pct", 0)
        lcp_content = u.get("lcp_content", []) or []
        same_global = u.get("same_as_global", False)
        prefix_match = u.get("prefix_match_with_global", 0)

        is_heavy = share >= heavy_pct
        no_chain = chain_len == 0

        klass = "user-block"
        if no_chain:
            klass += " no-chain"
        elif is_heavy:
            klass += " heavy"

        badges = []
        if is_heavy:
            badges.append('<span class="badge badge-heavy">heavy</span>')
        if no_chain:
            badges.append('<span class="badge badge-no-chain">no chain</span>')
        elif same_global:
            badges.append('<span class="badge badge-match">= global</span>')
        elif prefix_match == 0:
            badges.append('<span class="badge badge-unique">unique</span>')
        else:
            badges.append(
                f'<span class="badge badge-match">prefix:{prefix_match}</span>'
            )

        decoded_text = concat_decoded(lcp_content)
        n_decoded = sum(1 for b in lcp_content if b.get("decoded_text") is not None)
        prompt = prompt_block(decoded_text, chain_len, n_decoded)

        parts.append(f"""<div class="{klass}">
  <div class="user-header">
    <div class="user-id">{html.escape(str(uid))} {''.join(badges)}</div>
    <div class="user-meta">
      <span><b>requests:</b> {fmt_int(reqs)} ({share*100:.2f}% of total)</span>
      <span><b>chain_length:</b> {fmt_int(chain_len)}</span>
      <span><b>coverage:</b> {fmt_int(cov_count)} ({fmt_pct(cov_pct)})</span>
    </div>
  </div>
  {prompt}
</div>""")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def render_html(data: dict, source_path: Path, heavy_pct: float) -> str:
    title = "Per-User Chains Report"
    sections = [
        section_params(data),
        section_stats(data),
        section_user_aggregate(data),
        section_global_chain(data),
        section_per_user(data, heavy_pct),
    ]
    body = "\n".join(sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)} — {html.escape(source_path.name)}</title>
<style>{CSS}</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<div class="meta">source: {html.escape(str(source_path))}</div>
{body}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--input", required=True, type=Path,
                   help="per_user_chains*.json path")
    p.add_argument("--output", type=Path, default=None,
                   help="Output HTML path. Default: <input>.html")
    p.add_argument("--heavy-pct", type=float, default=0.20,
                   help="User request share threshold for heavy highlight "
                        "(default 0.20)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not args.input.exists():
        raise SystemExit(f"Input not found: {args.input}")

    output = args.output or args.input.with_suffix(".html")

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    html_str = render_html(data, args.input, args.heavy_pct)

    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        f.write(html_str)

    n_users = len(data.get("users", []) or [])
    g_chain_len = data.get("global_chain", {}).get("chain_length", 0)
    print(f"Rendered {n_users} users + global (chain_length={g_chain_len})")
    print(f"  → {output}")


if __name__ == "__main__":
    main()
