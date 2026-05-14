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

/* v2 (per_user_chains_html_redesign.md §5.9) — model-level report */
.caveat { background: #fed7d7; border-left: 4px solid #c53030;
  padding: 10px 16px; margin: 12px 0; font-size: 0.88em; color: #742a2a; }
.note { color: #718096; font-size: 0.85em; font-style: italic; margin-top: 6px; }
.missing-field { color: #c53030; font-style: italic; }

/* band color codes (复用 decision_matrix §9.2.2 阈值 0.30/0.60) */
.band-low    { color: #c53030; font-weight: 600; }
.band-normal { color: #b7791f; font-weight: 600; }
.band-high   { color: #2f855a; font-weight: 600; }

/* reuse_inversion warning + spike + WS state */
.inversion-warning { background: #fed7d7; border-left: 4px solid #c53030;
  padding: 10px 14px; margin: 10px 0; font-size: 0.9em; color: #742a2a;
  font-weight: 600; }
.spike-warning { background: #fff5f5; border-left: 4px solid #c53030;
  padding: 8px 14px; margin: 8px 0; font-size: 0.88em; }
.no-spike { background: #f7fafc; border-left: 4px solid #a0aec0;
  padding: 8px 14px; margin: 8px 0; font-size: 0.88em; color: #4a5568;
  font-style: italic; }
.ws-converged { background: #c6f6d5; border-left: 4px solid #2f855a;
  padding: 10px 14px; margin: 8px 0; font-size: 0.9em; color: #22543d; }
.ws-not-converged { background: #fed7d7; border-left: 4px solid #c53030;
  padding: 10px 14px; margin: 8px 0; font-size: 0.9em; color: #742a2a; }

/* v2 user skew table */
.skew-table { border-collapse: collapse; margin-top: 10px; font-size: 0.9em;
  width: 100%; }
.skew-table th, .skew-table td { padding: 6px 12px; text-align: right;
  border-bottom: 1px solid #e2e8f0; }
.skew-table th { background: #edf2f7; text-align: left;
  border-bottom: 2px solid #cbd5e0; }
.skew-table td.label { text-align: left; font-family: 'SF Mono', Consolas, monospace;
  color: #2d3748; word-break: break-all; max-width: 320px; }
.skew-table tr.row-low  { background: #fff5f5; }
.skew-table tr.row-high { background: #f0fff4; }

/* time-series block (chart + stats cards stacked) */
.ts-block { margin: 14px 0; }
.ts-chart { background: #f7fafc; border-radius: 4px; padding: 8px; }
.ts-quantile-grid { display: grid; grid-template-columns: repeat(5, 1fr);
  gap: 6px; margin-top: 8px; font-size: 0.85em; }
.ts-quantile-grid .stat-item { padding: 6px 10px; }
.ts-quantile-grid .stat-key { font-size: 0.75em; }
.ts-quantile-grid .stat-val { font-size: 1.1em; }

/* TODO placeholder (e.g., GB estimate awaiting kv_bytes_per_token) */
.tbd-note { color: #718096; font-style: italic; font-size: 0.85em;
  padding: 6px 10px; background: #f7fafc; border-radius: 3px; margin-top: 8px; }
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


def _band_class(value: float, lo: float = 0.30, hi: float = 0.60) -> str:
    """Map a value to band-low / band-normal / band-high CSS class."""
    if value < lo:
        return "band-low"
    if value > hi:
        return "band-high"
    return "band-normal"


# ---------------------------------------------------------------------------
# SVG charts (pure stdlib, no matplotlib) — v2 §5.3-§5.7
# ---------------------------------------------------------------------------

REF_COLORS = {
    "p50": "#3182ce",  # blue
    "p80": "#d69e2e",  # yellow
    "p95": "#dd6b20",  # orange
    "max": "#c53030",  # red
}


def _downsample(data: list[dict], target_n: int, x_key: str, y_key: str) -> list[dict]:
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


def svg_bar_chart_with_refs(
    data: list[dict], x_key: str, y_key: str,
    title: str, x_label: str, y_label: str,
    quantiles: dict,            # {p50, p80, p95, max} → y values for dashed refs
    width: int = 900, height: int = 220, max_bars: int = 200,
) -> str:
    """Bar chart with 4 dashed horizontal reference lines (P50/P80/P95/Max).

    Lines are labeled at the right edge in matching color.
    """
    if not data:
        return f'<svg class="chart-empty">empty: {html.escape(title)}</svg>'

    data = _downsample(data, max_bars, x_key, y_key)
    pad_l, pad_r, pad_t, pad_b = 60, 80, 28, 32  # extra right pad for ref labels
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    bar_w = max(1.0, plot_w / len(data))
    y_max = max(quantiles.get("max", 0), max((d[y_key] for d in data), default=1))
    if y_max == 0:
        y_max = 1

    # Bars
    bars = []
    for i, d in enumerate(data):
        h_px = d[y_key] / y_max * plot_h
        x = pad_l + i * bar_w
        y = pad_t + plot_h - h_px
        bars.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" height="{h_px:.2f}" '
            f'fill="#4299e1" opacity="0.85"/>'
        )

    # Y axis baseline ticks (0 + y_max)
    y_ticks = []
    for frac in (0, 0.5, 1):
        y = pad_t + plot_h - frac * plot_h
        v = y_max * frac
        y_ticks.append(
            f'<text x="{pad_l - 8}" y="{y + 3}" text-anchor="end" '
            f'font-size="10" fill="#718096">{v:g}</text>'
        )

    # Dashed reference lines (P50/P80/P95/Max)
    ref_elems = []
    for key in ("p50", "p80", "p95", "max"):
        v = quantiles.get(key)
        if v is None or v < 0:
            continue
        y = pad_t + plot_h - (v / y_max) * plot_h
        color = REF_COLORS[key]
        ref_elems.append(
            f'<line x1="{pad_l}" y1="{y:.2f}" x2="{pad_l + plot_w}" y2="{y:.2f}" '
            f'stroke="{color}" stroke-width="1.2" stroke-dasharray="4,3"/>'
            f'<text x="{pad_l + plot_w + 4}" y="{y + 3:.2f}" '
            f'font-size="10" fill="{color}" font-weight="600">'
            f'{key}={v:g}</text>'
        )

    # X axis labels (first / mid / last)
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
  <text x="{pad_l}" y="16" font-size="13" fill="#2d3748" font-weight="600">{html.escape(title)}</text>
  {' '.join(y_ticks)}
  {' '.join(bars)}
  {' '.join(ref_elems)}
  {' '.join(x_ticks)}
  <text x="{(pad_l + width - pad_r) / 2}" y="{height - 6}" text-anchor="middle"
        font-size="10" fill="#718096">{html.escape(x_label)}</text>
  <text x="14" y="{(pad_t + plot_h) / 2 + plot_h / 4}" font-size="10" fill="#718096"
        transform="rotate(-90, 14, {(pad_t + plot_h) / 2 + plot_h / 4})">{html.escape(y_label)}</text>
</svg>"""


def svg_cumulative_line(
    data: list[dict], x_key: str, y_key: str,
    title: str, x_label: str, y_label: str,
    t_half: Optional[int] = None,
    width: int = 900, height: int = 220, max_points: int = 500,
) -> str:
    """Cumulative line chart with final + half horizontal dashed refs + T_half marker."""
    if not data:
        return f'<svg class="chart-empty">empty: {html.escape(title)}</svg>'

    if len(data) > max_points:
        step = max(1, len(data) // max_points)
        data = data[::step]

    pad_l, pad_r, pad_t, pad_b = 70, 80, 28, 32
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

    # Horizontal dashed refs: final + half
    final_y = pad_t
    half_y = pad_t + plot_h / 2
    refs = [
        f'<line x1="{pad_l}" y1="{final_y}" x2="{pad_l + plot_w}" y2="{final_y}" '
        f'stroke="#a0aec0" stroke-width="1" stroke-dasharray="4,3"/>'
        f'<text x="{pad_l + plot_w + 4}" y="{final_y + 3}" font-size="10" '
        f'fill="#4a5568">final={y_max:,}</text>',
        f'<line x1="{pad_l}" y1="{half_y}" x2="{pad_l + plot_w}" y2="{half_y}" '
        f'stroke="#a0aec0" stroke-width="1" stroke-dasharray="4,3"/>'
        f'<text x="{pad_l + plot_w + 4}" y="{half_y + 3}" font-size="10" '
        f'fill="#4a5568">half={y_max // 2:,}</text>',
    ]

    # T_half vertical marker
    if t_half is not None:
        t_half_x = pad_l + max(0, t_half - x_min) / x_span * plot_w
        refs.append(
            f'<line x1="{t_half_x:.2f}" y1="{pad_t}" x2="{t_half_x:.2f}" y2="{pad_t + plot_h}" '
            f'stroke="#dd6b20" stroke-width="1" stroke-dasharray="3,2"/>'
            f'<text x="{t_half_x + 4:.2f}" y="{pad_t + plot_h - 4}" font-size="10" '
            f'fill="#dd6b20" font-weight="600">T_half={t_half}s</text>'
        )

    # Y ticks (0 + y_max)
    y_ticks = []
    for frac in (0, 0.5, 1):
        y = pad_t + plot_h - frac * plot_h
        v = y_max * frac
        y_ticks.append(
            f'<text x="{pad_l - 8}" y="{y + 3}" text-anchor="end" '
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
  <text x="{pad_l}" y="16" font-size="13" fill="#2d3748" font-weight="600">{html.escape(title)}</text>
  {' '.join(refs)}
  {' '.join(y_ticks)}
  <polyline points="{' '.join(points)}" fill="none" stroke="#c05621" stroke-width="1.8"/>
  {' '.join(x_ticks)}
  <text x="{(pad_l + width - pad_r) / 2}" y="{height - 6}" text-anchor="middle"
        font-size="10" fill="#718096">{html.escape(x_label)}</text>
</svg>"""


def svg_threshold_sweep(
    sweep: list[dict],
    width: int = 600, height: int = 220,
) -> str:
    """Line chart for threshold_sweep_points (21 (threshold, chain_length) pairs)."""
    if not sweep:
        return '<svg class="chart-empty">empty: threshold sweep</svg>'

    pad_l, pad_r, pad_t, pad_b = 50, 30, 28, 32
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    y_max = max(p["chain_length"] for p in sweep) or 1

    # x axis: threshold 0.0 → 1.0
    points = []
    for p in sweep:
        x = pad_l + p["threshold"] * plot_w
        y = pad_t + plot_h - p["chain_length"] / y_max * plot_h
        points.append(f"{x:.2f},{y:.2f}")

    # Y axis
    y_ticks = []
    for frac in (0, 0.5, 1):
        y = pad_t + plot_h - frac * plot_h
        v = y_max * frac
        y_ticks.append(
            f'<text x="{pad_l - 6}" y="{y + 3}" text-anchor="end" '
            f'font-size="10" fill="#718096">{int(v)}</text>'
        )

    # X axis ticks at 0.0, 0.25, 0.5, 0.75, 1.0
    x_ticks = []
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        x = pad_l + frac * plot_w
        x_ticks.append(
            f'<line x1="{x}" y1="{pad_t + plot_h}" x2="{x}" y2="{pad_t + plot_h + 3}" '
            f'stroke="#a0aec0" stroke-width="1"/>'
            f'<text x="{x}" y="{pad_t + plot_h + 14}" text-anchor="middle" '
            f'font-size="10" fill="#718096">{frac:.2f}</text>'
        )

    # Plateau height annotation (rightmost L value)
    plateau_y = pad_t + plot_h - y_max / y_max * plot_h
    plateau_label = (
        f'<text x="{pad_l + 4}" y="{plateau_y + 12}" font-size="10" '
        f'fill="#2b6cb0" font-weight="600">L={y_max} (主前缀自然深度)</text>'
    )

    return f"""<svg viewBox="0 0 {width} {height}" width="100%"
                style="background:#fff; max-width:{width}px;">
  <text x="{pad_l}" y="16" font-size="13" fill="#2d3748" font-weight="600">chain_length vs branch_threshold</text>
  {' '.join(y_ticks)}
  <polyline points="{' '.join(points)}" fill="none" stroke="#2b6cb0" stroke-width="1.8"/>
  {plateau_label}
  {' '.join(x_ticks)}
  <text x="{(pad_l + width - pad_r) / 2}" y="{height - 6}" text-anchor="middle"
        font-size="10" fill="#718096">branch_threshold</text>
</svg>"""


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

def section_model_overview(data: dict) -> str:
    """v2 §0: model-level key metrics + virtuoso caveat."""
    s = data.get("stats") or {}
    n_users = s.get("total_users", 0)
    ihr = s.get("ideal_hit_rate_aggregate", 0.0) or 0.0
    dur_min = s.get("trace_duration_minutes", 0.0) or 0.0
    g_unique = s.get("global_unique_blocks", 0) or 0
    inv_ratio = data.get("reuse_inversion_ratio", "—")
    rpm_q = data.get("requests_per_min_q") or {}
    urpm_q = data.get("new_unique_blocks_per_sec_q") or {}

    items = [
        ("n_users",                       fmt_int(n_users)),
        ("ideal_hit_rate (aggregate)",    f"{ihr*100:.2f}%"),
        ("global_unique_blocks",          fmt_int(g_unique)),
        ("trace_duration_minutes",        fmt_num(dur_min)),
        ("reuse_inversion_ratio",         str(inv_ratio)),
        ("req/min  (avg)",                fmt_num(rpm_q.get("avg") or 0)),
        ("new_block/s (avg)",             fmt_num(urpm_q.get("avg") or 0)),
    ]

    caveat = (
        '<div class="caveat">'
        '⚠️ <b>ideal_hit_rate_aggregate</b> 是<b>字节级 LCP 上界</b>，'
        '相对 vLLM 实际命中率系统性偏高 0–10pp，短 prompt 业务最高可达 30pp。'
        '决策应优先看 <b>ratio / 排序</b> 而非绝对值。详见 '
        '<code>docs/metrics_glossary.md §3</code>。'
        '</div>'
    )

    return f"<h2>0. 模型层关键指标</h2>{stat_grid(items)}{caveat}"


def section_user_skew(data: dict) -> str:
    """v2 §1: per-user skew table with hit_rate tri-color band + reuse_inversion warning."""
    users = data.get("users", []) or []
    inv_ratio = data.get("reuse_inversion_ratio", 1.0)
    inv = data.get("reuse_inversion", False)
    max_uid = data.get("max_hit_user")
    min_uid = data.get("min_hit_user")
    max_hit = data.get("max_hit_rate", 0.0)
    min_hit = data.get("min_hit_rate", 0.0)
    n_users = len(users)

    # Inversion warning (≥ 2.0 triggers)
    warning = ""
    if inv:
        warning = (
            f'<div class="inversion-warning">'
            f'⚠️ <b>reuse_inversion 触发</b> (ratio = {inv_ratio}x ≥ 2.0)<br>'
            f'max hit: <code>{html.escape(str(max_uid or "—"))}</code> ({max_hit:.4f})  '
            f'· min hit: <code>{html.escape(str(min_uid or "—"))}</code> ({min_hit:.4f})<br>'
            f'<span style="font-weight:normal">'
            f'建议：低 hit + 高流量 user 单独实例隔离 (A(1) isolation routing)，'
            f'防驱逐其他用户 chain。决策依据见 '
            f'<code>step3_algorithm_decision_matrix.md §9.2.3</code>。'
            f'</span>'
            f'</div>'
        )

    # Build table rows (sorted desc by request_count already in JSON)
    rows = []
    for u in users:
        uid = u.get("user_id", "")
        reqs = u.get("request_count", 0)
        pct = u.get("request_pct", 0.0)
        hit = u.get("ideal_hit_rate", 0.0) or 0.0
        chain_len = u.get("chain_length", 0)
        prefix_match = u.get("prefix_match_with_global", 0)
        same_global = u.get("same_as_global", False)

        if chain_len == 0:
            vs_global = "no_chain"
        elif same_global:
            vs_global = "= global"
        elif prefix_match == 0:
            vs_global = "unique"
        else:
            vs_global = f"prefix:{prefix_match}"

        band = _band_class(hit)
        row_cls = ""
        if band == "band-low":
            row_cls = "row-low"
        elif band == "band-high":
            row_cls = "row-high"

        uid_disp = uid if len(uid) <= 60 else uid[:57] + "..."
        rows.append(
            f'<tr class="{row_cls}">'
            f'<td class="label">{html.escape(uid_disp)}</td>'
            f'<td>{fmt_int(reqs)}</td>'
            f'<td>{pct:.2f}%</td>'
            f'<td class="{band}">{hit:.4f}</td>'
            f'<td>{chain_len}</td>'
            f'<td>{html.escape(vs_global)}</td>'
            f'</tr>'
        )

    table = (
        '<table class="skew-table">'
        '<thead><tr>'
        '<th>user_id</th><th>req_count</th><th>req_pct</th>'
        '<th>hit_rate</th><th>chain_len</th><th>vs_global</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )

    header_note = (
        f'<p class="note">User skew (n={n_users}, '
        f'reuse_inversion_ratio = {inv_ratio}x). '
        f'hit_rate 三色: <span class="band-low">&lt; 0.30 红</span> / '
        f'<span class="band-normal">0.30 – 0.60 黄</span> / '
        f'<span class="band-high">&gt; 0.60 绿</span> (复用 decision_matrix §9.2.2 阈值).</p>'
    )

    return f"<h2>1. 用户偏斜 (User Skew)</h2>{warning}{header_note}{table}"


def _ts_quantile_cards(q: dict, unit: str) -> str:
    """5 stat-item cards: avg → p50 → p80 → p95 → max (升序, v2 §6 决策)."""
    if not q:
        return ""
    items = [
        ("avg", f"{q.get('avg', 0):g}"),
        ("p50", f"{q.get('p50', 0):,}"),
        ("p80", f"{q.get('p80', 0):,}"),
        ("p95", f"{q.get('p95', 0):,}"),
        ("max", f"{q.get('max', 0):,}"),
    ]
    cells = "".join(
        f'<div class="stat-item">'
        f'<div class="stat-key">{html.escape(k)} {html.escape(unit)}</div>'
        f'<div class="stat-val">{html.escape(v)}</div></div>'
        for k, v in items
    )
    return f'<div class="ts-quantile-grid">{cells}</div>'


def section_traffic_timeseries(data: dict) -> str:
    """v2 §2: requests per minute + 4 dashed refs + 5 quantile cards."""
    ts = (data.get("time_series") or {}).get("requests_per_minute") or []
    q = data.get("requests_per_min_q") or {}
    chart = svg_bar_chart_with_refs(
        ts, "minute", "count",
        title=f"Requests per minute · avg={q.get('avg', 0):g} /min",
        x_label="minute (since trace start)",
        y_label="req",
        quantiles=q,
    )
    return f"""<h2>2. 请求量时序</h2>
<div class="ts-block">
  <div class="ts-chart">{chart}</div>
  {_ts_quantile_cards(q, "(req/min)")}
</div>"""


def section_traffic_spikes(data: dict) -> str:
    """v2 §2.1: spike events table + warning band."""
    spikes = data.get("traffic_spikes") or []
    cfg = data.get("spike_config") or {}
    win = cfg.get("window_minutes", 5)
    thr = cfg.get("threshold_multiplier", 5.0)

    if not spikes:
        warn = (
            f'<div class="no-spike">'
            f'无 ≥ ×{thr} 突变事件 (窗口 = {win} min)，流量稳定，无需弹性扩容。'
            f'</div>'
        )
        return f"<h2>2.1 流量突变时刻</h2>{warn}"

    rows = "".join(
        f'<tr>'
        f'<td class="label">+{s["window_start_seconds"]:,}s</td>'
        f'<td>{s["prev_window_count"]:,}</td>'
        f'<td>→ {s["this_window_count"]:,}</td>'
        f'<td>×{s["ratio_to_prev"]}</td>'
        f'</tr>'
        for s in spikes
    )
    warn = (
        f'<div class="spike-warning">'
        f'⚠️ <b>{len(spikes)} 个 ≥ ×{thr} 突变事件</b>'
        f' (窗口 = {win} min) → 建议弹性扩容应对流量突发。'
        f'</div>'
        f'<table class="skew-table">'
        f'<thead><tr><th>window_start</th><th>prev_count</th>'
        f'<th>this_count</th><th>ratio</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
    )
    return f"<h2>2.1 流量突变时刻</h2>{warn}"


def section_cache_pressure(data: dict) -> str:
    """v2 §3: new unique blocks per second + 4 dashed refs + 5 cards + GB TODO."""
    ts = (data.get("time_series") or {}).get("new_unique_blocks_per_second") or []
    q = data.get("new_unique_blocks_per_sec_q") or {}
    chart = svg_bar_chart_with_refs(
        ts, "second", "count",
        title=f"New unique blocks per second · avg={q.get('avg', 0):g} /s",
        x_label="second (since trace start)",
        y_label="blocks",
        quantiles=q,
    )
    tbd = (
        '<div class="tbd-note">'
        '<b>GB 估计 — TODO</b>: 待 model_report.json 补 <code>kv_bytes_per_token</code> '
        '+ <code>tokens_per_byte_avg</code> (或简化版 <code>gb_per_our_unique_block</code>) '
        '后启用。详见 <code>docs/metrics_glossary.md §6</code>。'
        '</div>'
    )
    return f"""<h2>3. Cache 压力 — new block/s</h2>
<div class="ts-block">
  <div class="ts-chart">{chart}</div>
  {_ts_quantile_cards(q, "(blocks/s)")}
  {tbd}
</div>"""


def _compute_ws_state(cumulative: list[dict], threshold_pct: float = 5.0,
                     tail_minutes: int = 5) -> tuple[str, str, Optional[int]]:
    """v2 §3.1: WS state from cumulative_unique_blocks slope analysis.

    Returns (status, message, t_half_seconds).
    status ∈ {"converged", "not_converged", "insufficient"}.

    Convergence rule (per_user_chains_html_redesign §6 decision 3):
      last `tail_minutes` slope / overall avg slope < threshold_pct % → converged
    """
    if len(cumulative) < 2:
        return ("insufficient", "数据不足，无法判断 WS 状态。", None)

    sorted_pts = sorted(cumulative, key=lambda d: d["second"])
    t_start = sorted_pts[0]["second"]
    t_end = sorted_pts[-1]["second"]
    total = sorted_pts[-1]["total"]
    duration = t_end - t_start

    if duration <= 0:
        return ("insufficient", "trace span 为 0，无法判断 WS 状态。", None)

    avg_slope = total / duration if duration else 0  # blocks/sec

    # Tail slope: last tail_minutes seconds
    tail_secs = tail_minutes * 60
    tail_cut = t_end - tail_secs
    tail_pts = [p for p in sorted_pts if p["second"] >= tail_cut]
    if len(tail_pts) < 2:
        return ("insufficient",
                f"trace span < {tail_minutes} min × 2, 无法判断尾部斜率。", None)
    tail_t = tail_pts[-1]["second"] - tail_pts[0]["second"]
    tail_delta = tail_pts[-1]["total"] - tail_pts[0]["total"]
    tail_slope = tail_delta / tail_t if tail_t else 0

    # T_half: time to reach total/2
    half_target = total / 2
    t_half = None
    for p in sorted_pts:
        if p["total"] >= half_target:
            t_half = p["second"] - t_start
            break

    if avg_slope == 0:
        return ("insufficient", "平均斜率为 0。", t_half)

    ratio = tail_slope / avg_slope
    threshold = threshold_pct / 100.0

    if ratio < threshold:
        msg = (
            f"✅ <b>已收敛</b> (最后 {tail_minutes}min 斜率 = "
            f"{ratio*100:.2f}% × 平均斜率, &lt; {threshold_pct}%) → "
            f"<b>C 池化容量保障可行</b>：cache 容量 ≥ {total:,} block "
            f"即可 hold 全部 unique。"
        )
        return ("converged", msg, t_half)
    else:
        msg = (
            f"⚠️ <b>持续上升</b> (最后 {tail_minutes}min 斜率 = "
            f"{ratio*100:.2f}% × 平均斜率, ≥ {threshold_pct}%) → "
            f"WS 无上限，<b>C 池化收益有限</b>，建议优先 A 路由 / B 淘汰。"
        )
        return ("not_converged", msg, t_half)


def section_cumulative_ws(data: dict) -> str:
    """v2 §3.1: cumulative unique blocks line chart + auto WS state."""
    cumulative = (data.get("time_series") or {}).get("cumulative_unique_blocks") or []
    if not cumulative:
        return '<h2>3.1 累计 unique blocks (WS)</h2><p class="note">无数据</p>'

    status, msg, t_half = _compute_ws_state(cumulative)
    final = cumulative[-1]["total"]
    chart = svg_cumulative_line(
        cumulative, "second", "total",
        title=f"Cumulative unique blocks · final = {final:,}",
        x_label="second (since trace start)",
        y_label="unique blocks",
        t_half=t_half,
    )
    state_cls = {
        "converged":     "ws-converged",
        "not_converged": "ws-not-converged",
        "insufficient":  "no-spike",
    }[status]
    state_block = f'<div class="{state_cls}">WS 状态: {msg}</div>'

    return f"""<h2>3.1 累计 unique blocks (WS)</h2>
<div class="ts-block">
  <div class="ts-chart">{chart}</div>
  {state_block}
</div>"""


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


def section_chain_content(data: dict) -> str:
    """v2 §4: threshold_sweep SVG + global LCP decoded content (跨用户 LCP).

    本 section 合并旧 §4 Global Chain + 新 threshold_sweep_points 渲染。
    跨用户 LCP 语义 = global chain (所有 user 共享的最长前缀)。
    """
    sweep = data.get("threshold_sweep_points") or []
    g = data.get("global_chain", {}) or {}
    chain_length = g.get("chain_length", 0)
    coverage_count = g.get("chain_coverage_count", 0)
    coverage_pct = g.get("chain_coverage_pct", 0)
    lcp_content = g.get("lcp_content", []) or []

    # §4.1 threshold sweep chart
    sweep_block = ""
    if sweep:
        sweep_svg = svg_threshold_sweep(sweep)
        sweep_block = f"""
<h3>4.1 chain_length vs branch_threshold sweep</h3>
<div class="ts-chart">{sweep_svg}</div>
<p class="note">L = 主前缀自然深度；陡降点 = 主 chain 最弱环节强度；
   平台中段 (远离陡降点) 是 branch_threshold 安全选值区。
   详见 <code>docs/metrics_glossary.md §4</code>。</p>
"""

    # §4.2 global chain (跨用户 LCP)
    metrics = stat_grid([
        ("chain_length", fmt_int(chain_length)),
        ("chain_coverage_count", fmt_int(coverage_count)),
        ("chain_coverage_pct", fmt_pct(coverage_pct)),
    ])
    decoded_text = concat_decoded(lcp_content)
    n_decoded = sum(1 for b in lcp_content if b.get("decoded_text") is not None)
    prompt = prompt_block(decoded_text, chain_length, n_decoded)
    semantic_note = (
        '<p class="note">跨用户 LCP = global chain (所有 user 共享的最长前缀)。'
        'chain_length = 0 表示用户间无跨用户共享前缀。</p>'
    )

    return f"""<h2>4. Chain 内容</h2>
{sweep_block}
<h3>4.2 跨用户 LCP (Global Chain)</h3>
{semantic_note}
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
        hit_rate = u.get("ideal_hit_rate", 0.0) or 0.0   # v2

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

        # v2: hit_rate tri-color (复用 decision_matrix §9.2.2 阈值 0.30/0.60)
        hit_band = _band_class(hit_rate)

        parts.append(f"""<div class="{klass}">
  <div class="user-header">
    <div class="user-id">{html.escape(str(uid))} {''.join(badges)}</div>
    <div class="user-meta">
      <span><b>requests:</b> {fmt_int(reqs)} ({share*100:.2f}% of total)</span>
      <span><b>hit_rate:</b> <span class="{hit_band}">{hit_rate:.4f}</span></span>
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

def section_appendix_raw(data: dict) -> str:
    """Appendix: original Params + Stats + User Aggregate sections.

    These are still useful for debugging but no longer top-level.
    """
    p_html = section_params(data)
    s_html = section_stats(data)
    a_html = section_user_aggregate(data)
    return f"""<h2>Appendix · 原始统计</h2>
<details><summary>展开查看 Params / Stats / User Aggregate</summary>
{p_html}
{s_html}
{a_html}
</details>"""


def render_html(data: dict, source_path: Path, heavy_pct: float) -> str:
    title = "Per-User Chains Report (v2)"
    # v2 section order (per_user_chains_html_redesign.md §5):
    #   §0 model overview → §1 user skew → §2 traffic ts → §2.1 spikes →
    #   §3 cache pressure → §3.1 WS state → §4 chain content (sweep + global) →
    #   §5 per-user chains → Appendix
    sections = [
        section_model_overview(data),
        section_user_skew(data),
        section_traffic_timeseries(data),
        section_traffic_spikes(data),
        section_cache_pressure(data),
        section_cumulative_ws(data),
        section_chain_content(data),
        section_per_user(data, heavy_pct),
        section_appendix_raw(data),
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
