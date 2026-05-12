#!/usr/bin/env python3
"""Step 1.5 — Per-user HTML report renderer.

Walks an output directory produced by per_user_report_analyzer.py and
renders one self-contained HTML per user. Charts are inline SVG (no JS,
no external assets), so the report is offline-safe.

Sections in each user's HTML:
  1. Banner            — user_id, key metrics, single-tenant flag if any
  2. Caveats           — timestamp second-precision warning (D5)
  3. Stats grid        — totals, ideal hit rate, unique blocks
  4. Time series       — requests/min, new unique block/s, cumulative WS
  5. Distributions     — inter-arrival gaps, LCP histogram
  6. Chain forest      — one card per chain with decoded content

Reusable across models; tokenizer-free; offline-safe.

Usage
-----
  python scripts/render_user_report_html.py \\
      --input-dir outputs/<dataset>/per_user_reports
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# CSS (matches render_chains_html.py palette)
# ---------------------------------------------------------------------------

CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  max-width: 1180px; margin: 24px auto; padding: 0 24px; color: #2d3748;
  line-height: 1.5; }
h1 { border-bottom: 2px solid #2d3748; padding-bottom: 8px; margin-bottom: 4px; }
h1 .subtle { color: #718096; font-size: 0.55em; font-weight: normal; }
h2 { color: #2b6cb0; margin-top: 36px; padding-bottom: 4px;
  border-bottom: 1px solid #cbd5e0; }
h3 { color: #2c5282; margin-top: 18px; }
.meta { background: #edf2f7; padding: 10px 16px; border-left: 4px solid #4299e1;
  font-family: 'SF Mono', Consolas, Menlo, monospace; font-size: 0.85em;
  word-break: break-all; }
.caveat { background: #fefcbf; border-left: 4px solid #d69e2e; padding: 10px 16px;
  margin: 12px 0; font-size: 0.9em; }
.tenant-tag { background: #c6f6d5; color: #22543d; padding: 2px 8px;
  border-radius: 3px; font-size: 0.7em; margin-left: 8px; vertical-align: middle; }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 10px; margin-top: 12px; }
.stat-item { background: #f7fafc; padding: 10px 14px; border-radius: 4px;
  border-left: 3px solid #cbd5e0; }
.stat-key { color: #718096; font-size: 0.8em; text-transform: uppercase;
  letter-spacing: 0.05em; }
.stat-value { font-size: 1.4em; font-weight: 600; color: #2d3748; }
.stat-sub { color: #4a5568; font-size: 0.85em; }
table { border-collapse: collapse; margin-top: 10px; font-size: 0.9em; }
th, td { padding: 6px 12px; text-align: right; border-bottom: 1px solid #e2e8f0; }
th { background: #edf2f7; text-align: left; }
td.label { text-align: left; color: #4a5568; }
.chart { background: #f7fafc; border-radius: 4px; padding: 8px; margin-top: 8px;
  overflow-x: auto; }
.chart-caveat { font-size: 0.78em; color: #718096; padding: 4px 8px; }
.chain-card { background: #f7fafc; border-left: 3px solid #4299e1;
  padding: 10px 16px; margin: 12px 0; border-radius: 4px; }
.chain-card.minor { border-left-color: #cbd5e0; opacity: 0.85; }
.chain-header { display: flex; flex-wrap: wrap; gap: 16px; font-size: 0.9em;
  color: #4a5568; margin-bottom: 6px; }
.chain-header .id { font-weight: 600; color: #2b6cb0; }
.chain-header .cov { font-weight: 600; color: #c05621; }
details { margin-top: 6px; }
summary { cursor: pointer; color: #2b6cb0; font-size: 0.9em; }
pre.decoded { background: #1a202c; color: #e2e8f0; padding: 10px 14px;
  border-radius: 3px; font-size: 0.78em; overflow-x: auto; white-space: pre-wrap;
  word-break: break-all; max-height: 320px; overflow-y: auto; }
.note { color: #718096; font-size: 0.85em; font-style: italic; }
"""


# ---------------------------------------------------------------------------
# SVG charts (pure stdlib, no matplotlib)
# ---------------------------------------------------------------------------

def _downsample(data: list[dict], target_n: int, x_key: str, y_key: str) -> list[dict]:
    """Aggregate sequence into <= target_n buckets by summing y values."""
    if len(data) <= target_n:
        return data
    step = max(1, len(data) // target_n)
    out = []
    for i in range(0, len(data), step):
        chunk = data[i:i + step]
        out.append({
            x_key: chunk[0][x_key],
            y_key: sum(d[y_key] for d in chunk),
        })
    return out


def svg_bar_chart(
    data: list[dict], x_key: str, y_key: str,
    title: str, x_label: str = "", y_label: str = "",
    width: int = 900, height: int = 180, max_bars: int = 200,
) -> str:
    """Bar chart as inline SVG; aggregates if data exceeds max_bars."""
    if not data:
        return f'<svg class="chart-empty">empty: {html.escape(title)}</svg>'

    data = _downsample(data, max_bars, x_key, y_key)
    pad_l, pad_r, pad_t, pad_b = 50, 12, 24, 28
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    bar_w = max(1.0, plot_w / len(data))
    y_max = max((d[y_key] for d in data), default=1)
    if y_max == 0:
        y_max = 1
    x_min = data[0][x_key]
    x_max = data[-1][x_key]

    bars = []
    for i, d in enumerate(data):
        h_px = d[y_key] / y_max * plot_h
        x = pad_l + i * bar_w
        y = pad_t + plot_h - h_px
        bars.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" height="{h_px:.2f}" '
            f'fill="#4299e1"/>'
        )

    # Y axis ticks
    y_ticks = []
    for frac in (0, 0.5, 1):
        v = y_max * frac
        y = pad_t + plot_h - frac * plot_h
        y_ticks.append(
            f'<line x1="{pad_l}" y1="{y}" x2="{pad_l + plot_w}" y2="{y}" '
            f'stroke="#e2e8f0" stroke-width="1"/>'
            f'<text x="{pad_l - 6}" y="{y + 3}" text-anchor="end" '
            f'font-size="10" fill="#718096">{v:g}</text>'
        )

    # X axis labels (just first / mid / last)
    x_ticks = []
    for frac, idx in [(0, 0), (0.5, len(data) // 2), (1, len(data) - 1)]:
        x = pad_l + frac * plot_w
        val = data[idx][x_key]
        x_ticks.append(
            f'<text x="{x}" y="{pad_t + plot_h + 14}" text-anchor="middle" '
            f'font-size="10" fill="#718096">{val}</text>'
        )

    return f"""<svg viewBox="0 0 {width} {height}" width="100%"
                    style="background:#fff; max-width:{width}px;">
  <text x="{pad_l}" y="14" font-size="12" fill="#2d3748" font-weight="600">{html.escape(title)}</text>
  <text x="{width - pad_r}" y="14" text-anchor="end" font-size="10" fill="#718096">{html.escape(y_label)}</text>
  {' '.join(y_ticks)}
  {' '.join(bars)}
  {' '.join(x_ticks)}
  <text x="{(pad_l + width - pad_r) / 2}" y="{height - 6}" text-anchor="middle" font-size="10" fill="#718096">{html.escape(x_label)}</text>
</svg>"""


def svg_line_chart(
    data: list[dict], x_key: str, y_key: str,
    title: str, x_label: str = "", y_label: str = "",
    width: int = 900, height: int = 180, max_points: int = 500,
) -> str:
    """Cumulative-style line chart."""
    if not data:
        return f'<svg class="chart-empty">empty: {html.escape(title)}</svg>'

    if len(data) > max_points:
        step = max(1, len(data) // max_points)
        data = data[::step]

    pad_l, pad_r, pad_t, pad_b = 60, 12, 24, 28
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    y_max = max((d[y_key] for d in data), default=1) or 1
    x_min = data[0][x_key]
    x_max = data[-1][x_key]
    x_span = max(1, x_max - x_min)

    points = []
    for d in data:
        x = pad_l + (d[x_key] - x_min) / x_span * plot_w
        y = pad_t + plot_h - d[y_key] / y_max * plot_h
        points.append(f"{x:.2f},{y:.2f}")

    y_ticks = []
    for frac in (0, 0.5, 1):
        v = y_max * frac
        y = pad_t + plot_h - frac * plot_h
        y_ticks.append(
            f'<line x1="{pad_l}" y1="{y}" x2="{pad_l + plot_w}" y2="{y}" '
            f'stroke="#e2e8f0" stroke-width="1"/>'
            f'<text x="{pad_l - 6}" y="{y + 3}" text-anchor="end" '
            f'font-size="10" fill="#718096">{int(v):,}</text>'
        )
    x_ticks = []
    for frac, idx in [(0, 0), (0.5, len(data) // 2), (1, len(data) - 1)]:
        x = pad_l + frac * plot_w
        val = data[idx][x_key]
        x_ticks.append(
            f'<text x="{x}" y="{pad_t + plot_h + 14}" text-anchor="middle" '
            f'font-size="10" fill="#718096">{val}</text>'
        )

    return f"""<svg viewBox="0 0 {width} {height}" width="100%"
                    style="background:#fff; max-width:{width}px;">
  <text x="{pad_l}" y="14" font-size="12" fill="#2d3748" font-weight="600">{html.escape(title)}</text>
  <text x="{width - pad_r}" y="14" text-anchor="end" font-size="10" fill="#718096">{html.escape(y_label)}</text>
  {' '.join(y_ticks)}
  <polyline points="{' '.join(points)}" fill="none" stroke="#c05621" stroke-width="1.5"/>
  {' '.join(x_ticks)}
  <text x="{(pad_l + width - pad_r) / 2}" y="{height - 6}" text-anchor="middle" font-size="10" fill="#718096">{html.escape(x_label)}</text>
</svg>"""


def svg_histogram(hist: list[dict], title: str, width: int = 700, height: int = 160) -> str:
    """LCP histogram. Each entry has lcp_low, lcp_high, count."""
    if not hist:
        return ""
    pad_l, pad_r, pad_t, pad_b = 50, 12, 24, 32
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    bar_w = plot_w / len(hist)
    y_max = max(d["count"] for d in hist) or 1

    bars = []
    labels = []
    for i, d in enumerate(hist):
        h_px = d["count"] / y_max * plot_h
        x = pad_l + i * bar_w
        y = pad_t + plot_h - h_px
        bars.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w - 1:.2f}" height="{h_px:.2f}" '
            f'fill="#48bb78"/>'
        )
        if i % max(1, len(hist) // 6) == 0 or i == len(hist) - 1:
            labels.append(
                f'<text x="{x + bar_w / 2}" y="{pad_t + plot_h + 14}" '
                f'text-anchor="middle" font-size="9" fill="#718096">{d["lcp_low"]}</text>'
            )

    y_ticks = []
    for frac in (0, 0.5, 1):
        v = y_max * frac
        y = pad_t + plot_h - frac * plot_h
        y_ticks.append(
            f'<line x1="{pad_l}" y1="{y}" x2="{pad_l + plot_w}" y2="{y}" '
            f'stroke="#e2e8f0" stroke-width="1"/>'
            f'<text x="{pad_l - 6}" y="{y + 3}" text-anchor="end" '
            f'font-size="10" fill="#718096">{int(v):,}</text>'
        )

    return f"""<svg viewBox="0 0 {width} {height}" width="100%"
                    style="background:#fff; max-width:{width}px;">
  <text x="{pad_l}" y="14" font-size="12" fill="#2d3748" font-weight="600">{html.escape(title)}</text>
  {' '.join(y_ticks)}
  {' '.join(bars)}
  {' '.join(labels)}
  <text x="{(pad_l + width - pad_r) / 2}" y="{height - 6}" text-anchor="middle"
        font-size="10" fill="#718096">LCP (blocks)</text>
</svg>"""


# ---------------------------------------------------------------------------
# HTML composition
# ---------------------------------------------------------------------------

def fmt_num(x) -> str:
    return f"{x:,}" if isinstance(x, int) else f"{x}"


def stat_item(key: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="stat-sub">{html.escape(sub)}</div>' if sub else ""
    return (
        f'<div class="stat-item">'
        f'<div class="stat-key">{html.escape(key)}</div>'
        f'<div class="stat-value">{html.escape(value)}</div>'
        f'{sub_html}</div>'
    )


def render_chain_card(chain: dict, top_cov: float, shadow_info: dict | None) -> str:
    cov = chain["coverage_pct"]
    klass = "chain-card" if cov >= max(0.5 * top_cov, 1.0) else "chain-card minor"
    bp = chain.get("branch_at_root_position")
    br = chain.get("branch_at_root_ratio")
    branch_str = (
        f"branches at pos {bp} (ratio {br:.3f})"
        if bp is not None else "no divergence from any sibling"
    )

    # v2 prefix coverage signal (portraits §3.7): show max prefix cov next to
    # leaf cov so chipset2-like asymmetry (max >> leaf) is immediately visible.
    max_pre = chain.get("max_prefix_coverage_pct")
    pre_str = (
        f"max_prefix_cov={max_pre:.2f}%" if max_pre is not None else ""
    )

    # v3 shadow group annotation (portraits §3.6): which chains share a
    # semantic prefix despite branch_pos=0.
    shadow_str = ""
    if shadow_info:
        gid = shadow_info.get("group_id")
        members = shadow_info.get("members", [])
        prefix_b = shadow_info.get("prefix_bytes")
        shadow_str = (
            f"shadow group #{gid} (chains {members}, "
            f"share first {prefix_b} bytes)"
        )

    concat_text = "".join(
        (b.get("decoded_text") or "") for b in chain.get("decoded_content", [])
    )
    decoded_html = html.escape(concat_text) if concat_text else (
        '<span class="note">no decoded content (sample request unavailable)</span>'
    )

    return f"""
<div class="{klass}">
  <div class="chain-header">
    <span class="id">chain #{chain['chain_id']}</span>
    <span>length={chain['chain_length']} blocks</span>
    <span class="cov">leaf_cov={chain['coverage_count']:,} ({chain['coverage_pct']:.2f}%)</span>
    <span>{html.escape(pre_str)}</span>
    <span>{html.escape(branch_str)}</span>
    <span>sample_request={html.escape(chain.get('sample_request_id') or '-')}</span>
    {'<span style="color:#805ad5;font-weight:600">' + html.escape(shadow_str) + '</span>' if shadow_str else ''}
  </div>
  <details>
    <summary>show decoded content ({len(concat_text):,} bytes)</summary>
    <pre class="decoded">{decoded_html}</pre>
  </details>
</div>
"""


def render_user_html(report: dict, forest: dict, total_users: int, total_requests: int) -> str:
    uid = report["user_id"]
    s = report["stats"]
    chains = forest.get("chains", [])
    top_cov = chains[0]["coverage_pct"] if chains else 0.0

    single_tenant = total_users == 1
    tenant_tag = (
        '<span class="tenant-tag">single-tenant model</span>' if single_tenant else ""
    )
    single_tenant_note = (
        '<p class="note">This is the only user_id on this model — '
        'per-user metrics equal full-model metrics.</p>' if single_tenant else ""
    )

    # Gaps & quantile tables
    gaps = report["inter_arrival_gaps_seconds"]
    new_q = report["new_unique_blocks_per_sec_q"]
    lcp_q = report["lcp_distribution"]["quantiles"]

    gap_table = f"""
<table>
  <tr><th>quantile</th><th>p50</th><th>p75</th><th>p80</th><th>p95</th><th>max</th></tr>
  <tr><td class="label">inter-arrival (s)</td>
    <td>{gaps['p50']:,}</td><td>{gaps['p75']:,}</td>
    <td>{gaps['p80']:,}</td><td>{gaps['p95']:,}</td><td>{gaps['max']:,}</td></tr>
  <tr><td class="label">new unique blocks / s</td>
    <td>{new_q['p50']:,}</td><td>-</td>
    <td>-</td><td>{new_q['p95']:,}</td><td>{new_q['max']:,}</td></tr>
  <tr><td class="label">per-request LCP</td>
    <td>{lcp_q['p50']:,}</td><td>-</td>
    <td>-</td><td>{lcp_q['p95']:,}</td><td>{lcp_q['max']:,}</td></tr>
</table>
"""

    ts = report["time_series"]
    chart_req_min = svg_bar_chart(
        ts["requests_per_minute"], "minute", "count",
        title="Requests per minute",
        x_label="minute (since trace start)",
        y_label="req",
    )
    chart_new_per_sec = svg_bar_chart(
        ts["new_unique_blocks_per_second"], "second", "count",
        title="New unique blocks per second",
        x_label="second (since trace start)",
        y_label="blocks",
    )
    chart_cumulative = svg_line_chart(
        ts["cumulative_unique_blocks"], "second", "total",
        title="Cumulative unique blocks (working set)",
        x_label="second (since trace start)",
        y_label="blocks",
    )
    chart_lcp_hist = svg_histogram(
        report["lcp_distribution"]["histogram"],
        title=f"Per-request LCP histogram "
              f"(bucket_size={lcp_q.get('bucket_size', 1)})",
    )

    # Build chain_id -> shadow info lookup (§3.6 v3 detection).
    overlap = forest.get("semantic_prefix_overlap") or {}
    shadow_groups = overlap.get("shadow_groups", [])
    matrix = overlap.get("matrix", [])
    block_size = (forest.get("params") or {}).get("block_size", 128)
    chain_to_shadow: dict[int, dict] = {}
    for gid, group in enumerate(shadow_groups):
        if not group:
            continue
        # Min pairwise prefix (bytes) among group members — floor of what's shared
        min_prefix_bytes = min(
            (matrix[a][b] for a in group for b in group if a != b),
            default=0,
        )
        for cid in group:
            chain_to_shadow[cid] = {
                "group_id":     gid,
                "members":      group,
                "prefix_bytes": min_prefix_bytes,
                "prefix_blocks_approx": min_prefix_bytes // block_size,
            }

    chain_cards = "".join(
        render_chain_card(c, top_cov, chain_to_shadow.get(c["chain_id"]))
        for c in chains
    )
    if not chain_cards:
        chain_cards = '<p class="note">No chains satisfied the multi-chain thresholds.</p>'

    # Shadow groups summary (only show if any detected)
    shadow_summary = ""
    if shadow_groups:
        rows = []
        for gid, group in enumerate(shadow_groups):
            info = chain_to_shadow[group[0]]
            b = info["prefix_bytes"]
            approx_blk = info["prefix_blocks_approx"]
            rows.append(
                f'<li>group #{gid}: chains {group} '
                f'share <strong>first {b} bytes</strong> '
                f'(≈ {approx_blk} blocks at block_size={block_size}; '
                f'byte-identical from raw prompt)</li>'
            )
        shadow_summary = (
            f'<div class="caveat" style="border-left-color:#805ad5">'
            f'<strong>Shadow groups detected ({len(shadow_groups)}):</strong> '
            f'these chains have branch_pos=0 but actually share a byte-identical '
            f'leading prefix that block_size chunking split mid-block. Step 3 '
            f'pin economics should treat each group as one shared-prefix unit.<ul>'
            f'{"".join(rows)}</ul></div>'
        )

    # Caveats from JSON
    caveats = report.get("caveats", [])
    caveat_html = "".join(
        f'<div class="caveat">{html.escape(c)}</div>' for c in caveats
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<title>Per-user report · {html.escape(uid)}</title>
<style>{CSS}</style>
</head><body>
<h1>Per-user research report
  <span class="subtle">user_id = {html.escape(uid)}</span>{tenant_tag}
</h1>
{single_tenant_note}
{caveat_html}

<h2>1. Key metrics</h2>
<div class="stat-grid">
  {stat_item("requests", f"{s['total_requests']:,}",
             f"{s['total_requests']/total_requests*100:.2f}% of {total_requests:,}")}
  {stat_item("ideal hit rate", f"{s['ideal_hit_rate']*100:.2f}%",
             f"vLLM block-level, user-internal")}
  {stat_item("total blocks", f"{s['total_blocks']:,}",
             f"empty_prompts={s['empty_prompts']:,}")}
  {stat_item("unique blocks", f"{s['unique_blocks']:,}",
             f"hit_blocks={s['hit_blocks']:,}")}
  {stat_item("trace span (s)",
             f"{(s['latest_timestamp'] or 0) - (s['earliest_timestamp'] or 0):,}",
             f"{s['earliest_timestamp']}–{s['latest_timestamp']}")}
  {stat_item("chains found", f"{forest['stats']['total_chains_after_max_cap']:,}",
             f"forest_seconds={forest['stats'].get('forest_seconds', 0):.2f}")}
</div>

<h2>2. Request arrival time series</h2>
<div class="chart">{chart_req_min}
  <div class="chart-caveat">Aggregated to ≤200 buckets. timestamp resolution = 1s (D5).</div>
</div>
{gap_table}

<h2>3. Cache insertion pressure</h2>
<div class="chart">{chart_new_per_sec}
  <div class="chart-caveat">Aggregated to ≤200 buckets. Same-second resolution applies.</div>
</div>
<div class="chart">{chart_cumulative}
  <div class="chart-caveat">Cumulative unique <code>prefix_path_key</code> — WS lower bound.</div>
</div>

<h2>4. Per-request LCP distribution</h2>
<div class="chart">{chart_lcp_hist}
  <div class="chart-caveat">LCP = matched prefix-path-key block count per incoming request.</div>
</div>

<h2>5. Chain forest</h2>
<div class="meta">
  multi-chain params: branch_thr={forest['params']['mc_branch_threshold']}
  cov_thr={forest['params']['mc_coverage_threshold']}
  min_length={forest['params']['mc_min_chain_length']}
  min_coverage={forest['params']['mc_min_chain_coverage']}
  max_chains={forest['params']['mc_max_chains']}
  block_size={forest['params'].get('block_size', 128)}
</div>
<div class="meta">
  pruning: raw={forest['stats']['total_chains_before_pruning']} →
  length={forest['stats']['total_chains_after_length_pruning']} →
  coverage={forest['stats']['total_chains_after_coverage_pruning']} →
  cap={forest['stats']['total_chains_after_max_cap']}
</div>
{shadow_summary}
{chain_cards}

</body></html>
"""


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--input-dir", required=True, type=Path,
        help="Output directory produced by per_user_report_analyzer.py "
             "(must contain user_summary.json + <user_id>/ subdirs)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    input_dir: Path = args.input_dir

    summary_path = input_dir / "user_summary.json"
    if not summary_path.exists():
        print(f"error: {summary_path} not found", file=__import__("sys").stderr)
        raise SystemExit(1)

    summary = json.load(open(summary_path, encoding="utf-8"))
    total_users = summary.get("total_users", 0)
    total_requests = summary.get("total_requests", 0)
    selected_users = [r["user_id"] for r in summary.get("selected_users", [])]

    print(f"Rendering {len(selected_users)} user report(s)...", flush=True)

    for uid in selected_users:
        from per_user_report_analyzer import safe_dirname
        udir = input_dir / safe_dirname(uid)
        report_path = udir / "user_report.json"
        forest_path = udir / "chain_forest.json"
        if not report_path.exists() or not forest_path.exists():
            print(f"  {uid}: missing report or forest JSON, skipping", flush=True)
            continue
        report = json.load(open(report_path, encoding="utf-8"))
        forest = json.load(open(forest_path, encoding="utf-8"))
        html_out = render_user_html(report, forest, total_users, total_requests)
        out_path = udir / "user_report.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_out)
        print(f"  {uid} → {out_path}", flush=True)


if __name__ == "__main__":
    main()
