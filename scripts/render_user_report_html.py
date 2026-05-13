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

/* Step 3 recommendation panel (decision matrix) */
.rec-panel { padding: 14px 18px; border-radius: 6px; margin: 14px 0; }
.rec-A { background: #ebf8ff; border-left: 5px solid #3182ce; }     /* routing  -> blue */
.rec-B { background: #f0fff4; border-left: 5px solid #38a169; }     /* eviction -> green */
.rec-C { background: #faf5ff; border-left: 5px solid #805ad5; }     /* pooling  -> purple */
.rec-D { background: #fffaf0; border-left: 5px solid #dd6b20; }     /* prompt   -> orange */
.rec-header { font-size: 1.05em; font-weight: 600; margin-bottom: 4px; }
.rec-header .companion { color: #4a5568; font-weight: normal; font-size: 0.85em; }
.rec-meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 6px; margin: 10px 0; font-size: 0.85em; }
.rec-meta-key { color: #718096; font-size: 0.8em; text-transform: uppercase;
  letter-spacing: 0.04em; }
.rec-meta-value { color: #2d3748; font-weight: 600; margin-top: 1px; }
.rec-difficulty-low      { color: #2f855a; }
.rec-difficulty-medium   { color: #b7791f; }
.rec-difficulty-high     { color: #c05621; }
.rec-difficulty-very_high { color: #c53030; }
.rec-section { margin-top: 8px; }
.rec-section h4 { color: #2d3748; font-size: 0.92em; margin: 8px 0 4px 0; }
.rec-section ul, .rec-section ol { margin: 0; padding-left: 22px; font-size: 0.88em; }
.rec-section li { margin-bottom: 2px; }

/* v2 (§9.2): subtype badges + classification color bands + missing-field flag */
.subtype-row { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 4px 0;
  font-size: 0.88em; }
.subtype-badge { padding: 3px 9px; border-radius: 12px; font-weight: 600;
  font-size: 0.84em; }
.subtype-A0 { background: #e2e8f0; color: #4a5568; }
.subtype-A1 { background: #fed7d7; color: #9b2c2c; }
.subtype-A2 { background: #bee3f8; color: #2c5282; }
.subtype-A3 { background: #c6f6d5; color: #22543d; }
.subtype-A4 { background: #feebc8; color: #9c4221; }
.subtype-B1 { background: #e2e8f0; color: #4a5568; }
.subtype-B2 { background: #c6f6d5; color: #22543d; }
.subtype-C1 { background: #e9d8fd; color: #553c9a; }
.subtype-C2 { background: #faf5ff; color: #6b46c1; }
.a4-warning { background: #fffaf0; border-left: 4px solid #dd6b20;
  padding: 8px 14px; margin: 8px 0; font-size: 0.88em; color: #7b341e; }
.tbd-note { color: #718096; font-style: italic; font-size: 0.85em; }
.anomaly-warning { background: #fefcbf; border-left: 4px solid #d69e2e;
  padding: 8px 14px; margin: 8px 0; font-size: 0.88em; color: #744210;
  font-weight: 600; }

/* §0 model-level stats: missing-field red highlight + band color codes */
.missing-field { background: #fed7d7; color: #9b2c2c; padding: 3px 8px;
  border-radius: 3px; font-style: italic; font-size: 0.85em; }
.band-high   { color: #2f855a; font-weight: 600; }
.band-low    { color: #c53030; font-weight: 600; }
.band-normal { color: #4a5568; }
.band-long   { color: #2b6cb0; font-weight: 600; }
.band-short  { color: #718096; }
.band-many   { color: #2b6cb0; font-weight: 600; }
.band-few    { color: #718096; }
.anomaly-row { background: #fefcbf !important; }

/* Spike events list */
.spike-list { background: #fff5f5; border-left: 4px solid #c53030;
  padding: 8px 14px; margin: 8px 0; font-size: 0.88em; }
.spike-list .none { color: #718096; font-style: italic; font-weight: normal; }
.spike-list table { margin-top: 4px; }
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


def render_classifications(report: dict) -> str:
    """v2 (§9.2): 6-dim classification badges + is_anomaly warning."""
    cls = report.get("classifications") or {}
    if not cls:
        return ""

    chain_summary = report.get("chain_forest_summary") or {}
    stats = report.get("stats") or {}
    hit_rate = stats.get("ideal_hit_rate", 0.0)
    cov_pct = chain_summary.get("dominant_chain_coverage_pct", 0.0)
    chain_ratio = chain_summary.get("chain_length_ratio", 0.0)
    chain_count = chain_summary.get("total_chains", 0)
    share = stats.get("share_of_model_unique") or 0.0

    def chip(label, val_str, band):
        return (
            f'<div class="stat-item">'
            f'<div class="stat-key">{html.escape(label)}</div>'
            f'<div class="stat-value band-{band}">{html.escape(val_str)}</div>'
            f'<div class="stat-sub">band = {html.escape(band)}</div>'
            f'</div>'
        )

    grid = "".join([
        chip("hit_rate",         f"{hit_rate*100:.2f}%",                 cls.get("hit_band", "normal")),
        chip("dominant cov",     f"{cov_pct:.2f}%",                       cls.get("cov_band", "normal")),
        chip("chain_length_ratio", f"{chain_ratio:.3f}",                  cls.get("chain_len_band", "short")),
        chip("share_of_model_unique", f"{share*100:.2f}%",                cls.get("unique_share_band", "normal")),
        chip("chain_count",      f"{chain_count}",                        cls.get("chain_count_band", "few")),
    ])

    anomaly_block = ""
    if cls.get("is_anomaly"):
        anomaly_block = (
            '<div class="anomaly-warning">'
            '⚠️ 反常: 长 chain + 低 cov + 低 hit — 建议人工检查 §5 chain decoded 内容, '
            '判断是否 wrapper boilerplate / 业务噪声而非真业务复用'
            '</div>'
        )

    return f"""
<h2>1.5 分类标签 (v2)</h2>
<div class="stat-grid">
  {grid}
</div>
{anomaly_block}
<p class="note">阈值见 model_report.json 的 thresholds 字段 (
   hit 30/60, cov 10/50, chain_len_ratio 0.3, unique_share 5/30, chain_count 3).</p>
"""


def render_chain_card(chain: dict, top_cov: float) -> str:
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
  </div>
  <details>
    <summary>show decoded content ({len(concat_text):,} bytes)</summary>
    <pre class="decoded">{decoded_html}</pre>
  </details>
</div>
"""


_ALGO_LABEL = {
    "A": "A 路由算法 (request 调度 / batch 聚合)",
    "B": "B 淘汰算法 (chain pin / per-user LRU / 多 chain 队列)",
    "C": "C KV cache 池化 (容量分区 / KV 量化 / 跨实例共享)",
    "D": "D prompt 修改 (业务侧重写 / 减少动态字段)",
}


def render_model_section(model_report: dict | None) -> str:
    """§0 model-level metrics panel + §0.1 traffic spike events.

    Renders manual-input fields (model_params_class / instance_count /
    cache_capacity_blocks) with a red "缺失" badge when None, per §9.2.4.
    """
    if not model_report:
        return ""

    def manual_field(key: str, label: str) -> str:
        val = model_report.get(key)
        if val is None:
            return (
                f'<div class="stat-item">'
                f'<div class="stat-key">{html.escape(label)}</div>'
                f'<div class="missing-field">⚠️ 缺失，需人工补</div>'
                f'</div>'
            )
        return stat_item(label, str(val))

    n_users = model_report.get("n_users", 0)
    multi = model_report.get("is_multi_tenant", False)
    ihr = model_report.get("ideal_hit_rate_aggregate", 0.0)
    rpm = model_report.get("rpm_avg", 0.0)
    urpm = model_report.get("unique_rpm_avg", 0.0)
    dur = model_report.get("trace_duration_minutes", 0.0)
    inv_ratio = model_report.get("reuse_inversion_ratio", "—")
    inv = model_report.get("reuse_inversion", False)
    spikes = model_report.get("traffic_spikes") or []
    spike_cfg = model_report.get("spike_config") or {}

    # ===== §0 panel =====
    grid_items = "".join([
        stat_item("n_users",                str(n_users),
                  "multi-tenant" if multi else "single-tenant"),
        stat_item("ideal_hit_rate (aggregate)", f"{ihr*100:.2f}%",
                  "sum(hit_blocks) / sum(total_blocks)"),
        stat_item("rpm avg",                f"{rpm:.1f}",
                  f"over {dur:.1f} min"),
        stat_item("unique_rpm avg",         f"{urpm:.1f}",
                  "Top-K user sum (upper bound of model unique)"),
        stat_item("reuse_inversion_ratio",  str(inv_ratio),
                  "triggered" if inv else "not triggered"),
        manual_field("model_params_class",  "model_params_class"),
        manual_field("instance_count",      "instance_count"),
        manual_field("cache_capacity_blocks", "cache_capacity_blocks"),
    ])

    # ===== §0.1 spike events =====
    if spikes:
        spike_rows = "".join(
            f'<tr><td class="label">window @ +{s["window_start_seconds"]:,}s</td>'
            f'<td>{s["prev_window_count"]:,}</td>'
            f'<td>→ {s["this_window_count"]:,}</td>'
            f'<td>×{s["ratio_to_prev"]}</td></tr>'
            for s in spikes
        )
        spike_block = f"""
<h3>0.1 流量突变时刻</h3>
<div class="spike-list">
  <b>{len(spikes)} 个突变事件</b>（窗口 = {spike_cfg.get('window_minutes', 5)} min,
  阈值 = ×{spike_cfg.get('threshold_multiplier', 5.0)}）
  <table>
    <tr><th>窗口起点</th><th>前窗口 req</th><th>本窗口 req</th><th>倍数</th></tr>
    {spike_rows}
  </table>
</div>
"""
    else:
        spike_block = f"""
<h3>0.1 流量突变时刻</h3>
<div class="spike-list">
  <span class="none">无 ≥ ×{spike_cfg.get('threshold_multiplier', 5.0)} 突变事件
    (窗口 = {spike_cfg.get('window_minutes', 5)} min)</span>
</div>
"""

    return f"""
<h2>0. 模型层指标</h2>
<div class="stat-grid">
  {grid_items}
</div>
<p class="note">人工补字段 (params_class / instance_count / cache_capacity_blocks) 显红时,
   请编辑 <code>model_report.json</code> 补全后重新渲染 HTML.
   说明见 docs/step3_algorithm_decision_matrix.md §9.2.1.</p>
{spike_block}
"""


def _subtype_badge_class(subtype: str) -> str:
    """Map subtype label to CSS badge class."""
    if "A(0)" in subtype or subtype.startswith("A0"):
        return "subtype-A0"
    if "A(1)" in subtype:
        return "subtype-A1"
    if "A(2)" in subtype:
        return "subtype-A2"
    if "A(3)" in subtype:
        return "subtype-A3"
    if "A(4)" in subtype:
        return "subtype-A4"
    if "B(1)" in subtype:
        return "subtype-B1"
    if "B(2)" in subtype:
        return "subtype-B2"
    if "C(1)" in subtype:
        return "subtype-C1"
    if "C(2)" in subtype:
        return "subtype-C2"
    return "subtype-A0"


def render_recommendation(rec: dict | None) -> str:
    """Render Step 3 algorithm recommendation as the HTML §6 panel (v2 subtype)."""
    if not rec or not rec.get("primary_algorithm"):
        return ""
    primary = rec["primary_algorithm"]
    companion = rec.get("companion_algorithm")
    business = rec.get("business_type") or "unknown"
    evidence = rec.get("business_evidence") or ""
    difficulty = rec.get("difficulty") or "unknown"
    estimated = rec.get("estimated_uplift") or {}
    reasons = rec.get("reasons") or []
    steps = rec.get("implementation_steps") or []

    # v2 subtype fields
    a_subtype = rec.get("a_subtype") or "A0 baseline"
    a_annotation = rec.get("a_annotation")
    b_subtype = rec.get("b_subtype") or "B(1) 默认 LRU"
    c_subtype = rec.get("c_subtype")

    primary_label = _ALGO_LABEL.get(primary, primary)
    companion_str = ""
    if companion:
        companion_str = f' <span class="companion">+ 辅菜：{html.escape(_ALGO_LABEL.get(companion, companion))}</span>'

    diff_class = f"rec-difficulty-{difficulty}"
    uplift_value = estimated.get("value") or "—"
    uplift_kind = estimated.get("kind") or "—"
    uplift_conf = estimated.get("confidence") or "—"

    # Subtype badges row
    subtype_badges = []
    subtype_badges.append(
        f'<span class="subtype-badge {_subtype_badge_class(a_subtype)}">'
        f'{html.escape(a_subtype)}</span>'
    )
    subtype_badges.append(
        f'<span class="subtype-badge {_subtype_badge_class(b_subtype)}">'
        f'{html.escape(b_subtype)}</span>'
    )
    if c_subtype:
        subtype_badges.append(
            f'<span class="subtype-badge {_subtype_badge_class(c_subtype)}">'
            f'{html.escape(c_subtype)}</span>'
        )
    subtype_row = (
        '<div class="subtype-row"><b>子类型 (v2):</b> '
        + " ".join(subtype_badges) + '</div>'
    )

    # A(4) warning
    a4_warning = ""
    if "A(4)" in a_subtype:
        a4_warning = (
            '<div class="a4-warning">'
            '⚠️ <b>A(4) 暂缓</b>: 大模型 (200B+ MOE), 待人工补 <code>instance_count</code> + '
            '<code>cache_capacity_blocks</code> 到 model_report.json 后, '
            '再决定具体 A 路由策略。当前阶段建议按 llm-d baseline (prefix_score) 部署.'
            '</div>'
        )
    elif a_annotation:
        a4_warning = f'<div class="note">A 说明: {html.escape(a_annotation)}</div>'

    # B(2) TBD note
    b2_note = ""
    if "B(2)" in b_subtype:
        b2_note = (
            '<div class="tbd-note">'
            'B(2) 淘汰打分公式 TBD: Step 2 实测调参 '
            '(输入: per-queue unique_rpm / hit_rate / chain_length)。'
            '当前工具仅给出 B(2) 触发，不固定具体淘汰打分。'
            '</div>'
        )

    reasons_html = "".join(f"<li>{html.escape(r)}</li>" for r in reasons)
    steps_html = "".join(f"<li>{html.escape(s)}</li>" for s in steps)

    return f"""
<h2>6. Step 3 算法推荐 (v2 子类型)</h2>
<div class="rec-panel rec-{primary}">
  <div class="rec-header">
    主菜：{html.escape(primary_label)}{companion_str}
  </div>
  {subtype_row}
  {a4_warning}
  {b2_note}

  <div class="rec-meta">
    <div>
      <div class="rec-meta-key">业务类型 (启发式)</div>
      <div class="rec-meta-value">{html.escape(business)}</div>
      <div class="note">{html.escape(evidence)}</div>
    </div>
    <div>
      <div class="rec-meta-key">预计难度</div>
      <div class="rec-meta-value {diff_class}">{html.escape(difficulty)}</div>
    </div>
    <div>
      <div class="rec-meta-key">预计提升 ({html.escape(uplift_kind)})</div>
      <div class="rec-meta-value">{html.escape(uplift_value)}</div>
      <div class="note">置信度: {html.escape(uplift_conf)}</div>
    </div>
  </div>

  <div class="rec-section">
    <h4>原因 (基于本 user 实测数据)</h4>
    <ul>{reasons_html}</ul>
  </div>

  <div class="rec-section">
    <h4>实施步骤</h4>
    <ol>{steps_html}</ol>
  </div>

  <div class="note" style="margin-top: 10px">
    决策规则: <a href="../../../docs/step3_algorithm_decision_matrix.md">step3_algorithm_decision_matrix.md</a> §9.2;
    业务类型识别是启发式, 边界 case 请人工 inspect §5 chain decoded content;
    提升估计基于 Step 1 信号, Step 2 实测前不是承诺.
  </div>
</div>
"""


def render_user_html(
    report: dict, forest: dict,
    total_users: int, total_requests: int,
    model_report: dict | None = None,
) -> str:
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

    chain_cards = "".join(render_chain_card(c, top_cov) for c in chains)
    if not chain_cards:
        chain_cards = '<p class="note">No chains satisfied the multi-chain thresholds.</p>'

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

{render_model_section(model_report)}

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

{render_classifications(report)}

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
{chain_cards}

{render_recommendation(report.get("step3_recommendation"))}

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

    # v2: load model_report.json (optional — older outputs may not have one)
    model_report_path = input_dir / "model_report.json"
    model_report: dict | None = None
    if model_report_path.exists():
        try:
            model_report = json.load(open(model_report_path, encoding="utf-8"))
        except Exception as e:
            print(f"warning: failed to load model_report.json: {e}", flush=True)
    else:
        print(
            "warning: model_report.json missing (re-run analyzer to enable §0 + §0.1)",
            flush=True,
        )

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
        html_out = render_user_html(
            report, forest, total_users, total_requests,
            model_report=model_report,
        )
        out_path = udir / "user_report.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_out)
        print(f"  {uid} → {out_path}", flush=True)


if __name__ == "__main__":
    main()
