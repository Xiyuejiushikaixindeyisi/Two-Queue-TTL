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

/* v3 (user_report_html_redesign.md): banner + horizontal bar compare + shadow + queue */
.algo-diff-banner { background: #ebf8ff; border-left: 5px solid #3182ce;
  padding: 12px 18px; margin: 14px 0; font-size: 0.9em; color: #2c5282; }
.encoder-banner-token { background: #fef5e7; border-left: 5px solid #d69e2e;
  padding: 12px 18px; margin: 14px 0; font-size: 0.9em; color: #744210; }
.encoder-banner-byte { background: #f7fafc; border-left: 5px solid #a0aec0;
  padding: 10px 18px; margin: 14px 0; font-size: 0.85em; color: #4a5568; }
.hbar-row { display: grid; grid-template-columns: 80px 1fr 120px;
  gap: 8px; align-items: center; margin: 4px 0; font-size: 0.88em; }
.hbar-label { font-weight: 600; color: #4a5568; }
.hbar-track { background: #edf2f7; height: 18px; border-radius: 3px; position: relative; }
.hbar-fill-user { background: #3182ce; height: 100%; border-radius: 3px; }
.hbar-fill-model { background: #a0aec0; height: 100%; border-radius: 3px; }
.hbar-value { font-family: 'SF Mono', Consolas, monospace; font-size: 0.85em;
  color: #2d3748; text-align: right; }
.compare-diagnosis { padding: 10px 14px; margin: 10px 0; border-radius: 4px;
  font-size: 0.9em; font-weight: 500; }
.compare-pollution { background: #fed7d7; border-left: 4px solid #c53030;
  color: #742a2a; }
.compare-benign { background: #c6f6d5; border-left: 4px solid #2f855a;
  color: #22543d; }
.compare-neutral { background: #f7fafc; border-left: 4px solid #a0aec0;
  color: #4a5568; }
.shadow-warning { background: #fed7d7; border-left: 4px solid #c53030;
  padding: 10px 14px; margin: 10px 0; font-size: 0.88em; color: #742a2a; }
.queue-count-row { background: #f0fff4; border-left: 4px solid #38a169;
  padding: 8px 14px; margin: 8px 0; font-size: 0.9em; color: #22543d; }
.lcp-anomaly-table { font-size: 0.85em; margin-top: 8px; }
.lcp-anomaly-table th { background: #edf2f7; padding: 6px 10px;
  text-align: left; border-bottom: 2px solid #cbd5e0; }
.lcp-anomaly-table td { padding: 6px 10px; border-bottom: 1px solid #e2e8f0; }
.diagnostic-note { background: #fffaf0; border-left: 4px solid #dd6b20;
  padding: 8px 14px; margin: 8px 0; font-size: 0.88em; color: #7b341e; }
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
    width: int = 900, height: int = 220, max_bars: int = 200,
    quantiles: dict | None = None,
    extra_ref_line: dict | None = None,
) -> str:
    """Bar chart as inline SVG.

    v3 enhancements:
      - `quantiles`: {p50, p80, p95, max} → draws 4 dashed user reference lines
        (P50蓝/P80黄/P95橙/Max红)
      - `extra_ref_line`: {value, label, color} → 1 extra dashed line
        (typically the model p50 in green for cross-comparison)
    """
    if not data:
        return f'<svg class="chart-empty">empty: {html.escape(title)}</svg>'

    data = _downsample(data, max_bars, x_key, y_key)
    pad_l, pad_r, pad_t, pad_b = 60, 80, 28, 32   # extra right pad for ref labels
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    bar_w = max(1.0, plot_w / len(data))
    y_max_data = max((d[y_key] for d in data), default=1)
    y_max_refs = (quantiles or {}).get("max", 0) if quantiles else 0
    y_max_extra = (extra_ref_line or {}).get("value", 0) if extra_ref_line else 0
    y_max = max(y_max_data, y_max_refs, y_max_extra, 1)

    bars = []
    for i, d in enumerate(data):
        h_px = d[y_key] / y_max * plot_h
        x = pad_l + i * bar_w
        y = pad_t + plot_h - h_px
        bars.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" height="{h_px:.2f}" '
            f'fill="#4299e1" opacity="0.85"/>'
        )

    # Y axis ticks (0 / 0.5 / 1)
    y_ticks = []
    for frac in (0, 0.5, 1):
        v = y_max * frac
        y = pad_t + plot_h - frac * plot_h
        y_ticks.append(
            f'<text x="{pad_l - 8}" y="{y + 3}" text-anchor="end" '
            f'font-size="10" fill="#718096">{v:g}</text>'
        )

    # v3: 4 user dashed reference lines
    REF_COLORS_V3 = {"p50": "#3182ce", "p80": "#d69e2e",
                      "p95": "#dd6b20", "max": "#c53030"}
    ref_elems = []
    if quantiles:
        for key in ("p50", "p80", "p95", "max"):
            v = quantiles.get(key)
            if v is None or v < 0:
                continue
            y = pad_t + plot_h - (v / y_max) * plot_h
            color = REF_COLORS_V3[key]
            ref_elems.append(
                f'<line x1="{pad_l}" y1="{y:.2f}" x2="{pad_l + plot_w}" y2="{y:.2f}" '
                f'stroke="{color}" stroke-width="1.2" stroke-dasharray="4,3"/>'
                f'<text x="{pad_l + plot_w + 4}" y="{y + 3:.2f}" '
                f'font-size="10" fill="{color}" font-weight="600">'
                f'{key}={v:g}</text>'
            )

    # v3: extra ref line (model_p50 in green)
    if extra_ref_line:
        v = extra_ref_line.get("value", 0) or 0
        label = extra_ref_line.get("label", "model")
        color = extra_ref_line.get("color", "#2f855a")
        if v > 0:
            y = pad_t + plot_h - (v / y_max) * plot_h
            ref_elems.append(
                f'<line x1="{pad_l}" y1="{y:.2f}" x2="{pad_l + plot_w}" y2="{y:.2f}" '
                f'stroke="{color}" stroke-width="1.5" stroke-dasharray="6,3"/>'
                f'<text x="{pad_l + plot_w + 4}" y="{y + 3:.2f}" '
                f'font-size="10" fill="{color}" font-weight="600">'
                f'{label}={v:g}</text>'
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
  <text x="{(pad_l + width - pad_r) / 2}" y="{height - 6}" text-anchor="middle" font-size="10" fill="#718096">{html.escape(x_label)}</text>
</svg>"""


def svg_horizontal_bar_compare(
    user_value: float, model_value: float, label: str,
    user_fmt: str = "{:.4f}", model_fmt: str = "{:.4f}",
    width: int = 700, max_value: float | None = None,
) -> str:
    """v3 §2: 2 stacked horizontal bars comparing user vs model value."""
    max_v = max_value if max_value else max(user_value, model_value, 1) * 1.1
    if max_v <= 0:
        max_v = 1
    user_pct = min(100, user_value / max_v * 100)
    model_pct = min(100, model_value / max_v * 100)
    diff = user_value - model_value
    diff_str = (
        f"(+{diff:.3f})" if diff > 0 else
        f"({diff:.3f})" if diff < 0 else "(=)"
    )
    return f"""<div class="hbar-block" style="margin: 12px 0;">
  <div style="font-weight:600; margin-bottom:4px; color:#2d3748;">{html.escape(label)}</div>
  <div class="hbar-row">
    <div class="hbar-label">user</div>
    <div class="hbar-track"><div class="hbar-fill-user" style="width:{user_pct:.1f}%"></div></div>
    <div class="hbar-value">{html.escape(user_fmt.format(user_value))} {html.escape(diff_str)}</div>
  </div>
  <div class="hbar-row">
    <div class="hbar-label">model</div>
    <div class="hbar-track"><div class="hbar-fill-model" style="width:{model_pct:.1f}%"></div></div>
    <div class="hbar-value">{html.escape(model_fmt.format(model_value))}</div>
  </div>
</div>"""


def svg_cdf_log_x(
    cdf_points: list[dict], quantiles: dict,
    width: int = 900, height: int = 220,
) -> str:
    """v3 §6: reuse_time CDF on log-x axis with p50/p80/p95 markers."""
    if not cdf_points:
        return '<svg class="chart-empty">empty: reuse_time CDF</svg>'

    pad_l, pad_r, pad_t, pad_b = 70, 80, 28, 32
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    import math
    xs = [max(1, p["t_seconds"]) for p in cdf_points]  # avoid log(0)
    log_xs = [math.log10(x) for x in xs]
    log_min = min(log_xs)
    log_max = max(log_xs)
    log_span = max(0.1, log_max - log_min)

    points = []
    for log_x, p in zip(log_xs, cdf_points):
        x = pad_l + (log_x - log_min) / log_span * plot_w
        y = pad_t + plot_h - (p["cumulative_pct"] / 100.0) * plot_h
        points.append(f"{x:.2f},{y:.2f}")

    # Y ticks: 0/50/100
    y_ticks = []
    for pct in (0, 50, 100):
        y = pad_t + plot_h - pct / 100.0 * plot_h
        y_ticks.append(
            f'<line x1="{pad_l}" y1="{y}" x2="{pad_l + plot_w}" y2="{y}" '
            f'stroke="#e2e8f0" stroke-width="1"/>'
            f'<text x="{pad_l - 6}" y="{y + 3}" text-anchor="end" '
            f'font-size="10" fill="#718096">{pct}%</text>'
        )

    # X ticks at decade boundaries (1, 10, 100, 1000, ...)
    x_ticks = []
    decade_start = int(math.floor(log_min))
    decade_end = int(math.ceil(log_max))
    for d in range(decade_start, decade_end + 1):
        x_val = 10 ** d
        x_pos = pad_l + (d - log_min) / log_span * plot_w
        if 0 <= x_pos - pad_l <= plot_w + 1:
            x_ticks.append(
                f'<line x1="{x_pos}" y1="{pad_t + plot_h}" x2="{x_pos}" y2="{pad_t + plot_h + 3}" '
                f'stroke="#a0aec0" stroke-width="1"/>'
                f'<text x="{x_pos}" y="{pad_t + plot_h + 14}" text-anchor="middle" '
                f'font-size="10" fill="#718096">{x_val}s</text>'
            )

    # Quantile markers: P50 / P80 / P95 vertical lines (clipped to chart)
    qmarks = []
    for key, color in [("p50", "#3182ce"), ("p80", "#d69e2e"), ("p95", "#dd6b20")]:
        v = quantiles.get(key, 0) or 0
        if v <= 0:
            continue
        lv = math.log10(max(1, v))
        if lv < log_min or lv > log_max:
            continue
        x = pad_l + (lv - log_min) / log_span * plot_w
        qmarks.append(
            f'<line x1="{x:.2f}" y1="{pad_t}" x2="{x:.2f}" y2="{pad_t + plot_h}" '
            f'stroke="{color}" stroke-width="1.2" stroke-dasharray="4,3"/>'
            f'<text x="{x:.2f}" y="{pad_t - 4}" text-anchor="middle" '
            f'font-size="10" fill="{color}" font-weight="600">{key}={v}s</text>'
        )

    return f"""<svg viewBox="0 0 {width} {height}" width="100%"
                style="background:#fff; max-width:{width}px;">
  <text x="{pad_l}" y="16" font-size="13" fill="#2d3748" font-weight="600">
    Reuse time CDF (block-level, log-scale x)
  </text>
  {' '.join(y_ticks)}
  <polyline points="{' '.join(points)}" fill="none" stroke="#805ad5" stroke-width="1.8"/>
  {' '.join(qmarks)}
  {' '.join(x_ticks)}
  <text x="{(pad_l + width - pad_r) / 2}" y="{height - 6}" text-anchor="middle"
        font-size="10" fill="#718096">reuse interval (seconds, log scale)</text>
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
    """v3 §1 model-level metrics — 去人工补字段, 加 p80, 修 n_users 含义."""
    if not model_report:
        return ""

    n_users_total = model_report.get("n_users_total", model_report.get("n_users", 0))
    n_users_sel = model_report.get("n_users", 0)
    multi = model_report.get("is_multi_tenant", False)
    ihr = model_report.get("ideal_hit_rate_aggregate", 0.0)
    rpm = model_report.get("rpm_avg", 0.0)
    urpm = model_report.get("unique_rpm_avg", 0.0)
    dur = model_report.get("trace_duration_minutes", 0.0)
    inv_ratio = model_report.get("reuse_inversion_ratio", "—")
    inv = model_report.get("reuse_inversion", False)

    rpm_q = model_report.get("requests_per_min_q") or {}
    nps_q = model_report.get("new_unique_blocks_per_sec_q") or {}
    rpm_p80 = rpm_q.get("p80", "—")
    nps_p80 = nps_q.get("p80", "—")

    # v3 bug diagnostic
    caveat = model_report.get("trace_duration_caveat")
    ts_failed = model_report.get("ts_parse_failed_count", 0)

    caveat_block = ""
    if caveat or ts_failed > 0:
        msgs = []
        if caveat:
            msgs.append(f"⚠️ {caveat}")
        if ts_failed > 0 and not (caveat and "ts parse" in caveat):
            msgs.append(f"⚠️ {ts_failed:,} 行 timestamp 解析失败 (fallback 0)")
        caveat_block = (
            '<div class="caveat">' + "<br>".join(html.escape(m) for m in msgs) +
            "<br>详见 <code>docs/user_report_html_redesign.md §4.1 bug 排查</code></div>"
        )

    grid_items = "".join([
        stat_item("n_users (total)",        str(n_users_total),
                  f"selected: {n_users_sel}; "
                  + ("multi-tenant" if multi else "single-tenant")),
        stat_item("ideal_hit_rate",         f"{ihr*100:.2f}%",
                  "aggregate (sum hit / sum total); 字节级上界, 详见 metrics_glossary §3"),
        stat_item("rpm avg + p80",          f"{rpm:.1f} / {rpm_p80}",
                  f"over {dur:.1f} min"),
        stat_item("unique_rpm avg",         f"{urpm:.1f}",
                  "Top-K user sum (upper bound)"),
        stat_item("new_block/s p80",        f"{nps_p80}",
                  "model-level cache pressure baseline"),
        stat_item("reuse_inversion_ratio",  str(inv_ratio),
                  "triggered" if inv else "not triggered"),
    ])

    return f"""
<h2>1. 模型层指标</h2>
{caveat_block}
<div class="stat-grid">
  {grid_items}
</div>
<p class="note">本节为对比基准: §4 / §5 时序图会引入 model_p50 绿色参考线;
   §2 用户偏斜会以此为基准做水平条对比.</p>
"""


def render_user_model_compare(report: dict, model_report: dict | None) -> str:
    """v3 §2: user vs model 3 水平条 + 文字诊断."""
    if not model_report:
        return ""

    s = report.get("stats") or {}
    u_hit = s.get("ideal_hit_rate", 0.0) or 0.0
    u_avg_blocks = s.get("avg_blocks_per_request", 0.0) or 0.0
    u_share = s.get("share_of_model_unique") or 0.0

    m_hit = model_report.get("ideal_hit_rate_aggregate", 0.0) or 0.0
    # model-level avg blocks/req: from model_report (if not stored, derive)
    m_total_blocks = (
        sum((u.get("stats") or {}).get("total_blocks", 0)
            for u in [report]) if False else None
    )
    # Approx: model avg ≈ aggregate via stats; here use top-K average as proxy
    # (we don't carry per-model avg_blocks_per_request — leave 0 if unknown)
    m_avg_blocks = 0.0  # not directly in model_report; render only user vs model hit + share

    # Build 3 bars
    bar_hit = svg_horizontal_bar_compare(
        u_hit, m_hit, "Hit rate (user vs model aggregate)",
        user_fmt="{:.4f}", model_fmt="{:.4f}",
        max_value=max(u_hit, m_hit, 1.0),
    )
    bar_share = svg_horizontal_bar_compare(
        u_share * 100, 100.0, "Unique block 占比 (% of Top-K sum)",
        user_fmt="{:.2f}%", model_fmt="{:.2f}%",
        max_value=100.0,
    )
    # avg_blocks compare omitted if model unknown
    bar_blocks_avg = ""
    if u_avg_blocks > 0:
        bar_blocks_avg = (
            f'<div class="hbar-block" style="margin: 12px 0;">'
            f'<div style="font-weight:600;color:#2d3748;">Avg blocks/req (user only — '
            f'model 端未聚合, 留作后续 Phase E)</div>'
            f'<div class="hbar-row">'
            f'<div class="hbar-label">user</div>'
            f'<div class="hbar-track"><div class="hbar-fill-user" style="width:100%"></div></div>'
            f'<div class="hbar-value">{u_avg_blocks:.2f}</div></div>'
            f'</div>'
        )

    # Diagnosis
    hit_diff = u_hit - m_hit
    diagnosis = ""
    if u_hit < m_hit and u_share >= 0.30:
        diagnosis = (
            '<div class="compare-diagnosis compare-pollution">'
            f'⚠️ <b>污染源</b>: hit_rate ({u_hit:.4f}) 低于 model ({m_hit:.4f}, '
            f'{hit_diff:+.3f}) 且 unique 占比 {u_share*100:.1f}% (≥ 30%). '
            f'该 user 大量写入 + 低复用 → 驱逐其他 user chain. '
            f'建议 A(1) isolation routing (隔离独立实例).'
            '</div>'
        )
    elif u_hit > m_hit and u_share <= 0.05:
        diagnosis = (
            '<div class="compare-diagnosis compare-benign">'
            f'✅ <b>良性轻量</b>: hit_rate ({u_hit:.4f}) 高于 model ({m_hit:.4f}, '
            f'{hit_diff:+.3f}) 且 unique 占比 {u_share*100:.1f}% (≤ 5%). '
            f'高复用 + 低 cache 占用 → 现状 LRU 友好.'
            '</div>'
        )
    else:
        diagnosis = (
            '<div class="compare-diagnosis compare-neutral">'
            f'中性: hit_rate {u_hit:.4f} vs model {m_hit:.4f} ({hit_diff:+.3f}), '
            f'unique 占比 {u_share*100:.1f}%. 见 §1.5 / §9 分类建议.'
            '</div>'
        )

    return f"""
<h2>2. user vs model 对比</h2>
{bar_hit}
{bar_share}
{bar_blocks_avg}
{diagnosis}
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
        # v3 §9: recommended_queue_count (B(2) 子类型触发时显示)
        q_count = rec.get("recommended_queue_count")
        if q_count is not None:
            b2_note += (
                f'<div class="queue-count-row">'
                f'💡 <b>建议队列数: {q_count}</b> '
                f'(仅算 cov ≥ 10% 的 chain, 避免维护成本高 / cache 占用过大). '
                f'详见 <code>user_report_html_redesign.md §9</code>.'
                f'</div>'
            )

    reasons_html = "".join(f"<li>{html.escape(r)}</li>" for r in reasons)
    steps_html = "".join(f"<li>{html.escape(s)}</li>" for s in steps)

    return f"""
<h2>9. Step 3 算法推荐 (v2 子类型)</h2>
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


def _padded_quantile(values: list[int], total_buckets: int) -> dict:
    """v3: quantile over values padded with zeros to total_buckets."""
    sorted_v = sorted(values)
    denom = max(total_buckets, len(sorted_v)) or 1
    n_zeros = max(0, denom - len(sorted_v))
    sum_v = sum(sorted_v)

    def _q(pct: float) -> int:
        idx = (denom - 1) * pct / 100.0
        lo = int(idx)
        if lo < n_zeros:
            return 0
        return sorted_v[lo - n_zeros] if sorted_v else 0

    return {
        "avg": round(sum_v / denom, 4) if denom else 0.0,
        "p50": _q(50), "p80": _q(80), "p95": _q(95),
        "max": sorted_v[-1] if sorted_v else 0,
    }


def _ts_quantile_cards_v3(q: dict, unit: str) -> str:
    """v3 §4/§5: 5 stat-item cards (升序 avg → p50 → p80 → p95 → max)."""
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
        f'<div class="stat-value">{html.escape(v)}</div></div>'
        for k, v in items
    )
    return f'<div class="ts-quantile-grid">{cells}</div>'


def render_user_metrics(report: dict, total_requests: int) -> str:
    """v3 §3: user metrics (流量占比 + unique 占比, 去 trace_span / chains_found)."""
    s = report.get("stats") or {}
    reqs = s.get("total_requests", 0)
    req_pct = reqs / total_requests * 100 if total_requests else 0.0
    share = s.get("share_of_model_unique") or 0.0

    items = [
        stat_item("requests", f"{reqs:,}",
                  f"{req_pct:.2f}% of {total_requests:,}"),
        stat_item("ideal hit rate", f"{s.get('ideal_hit_rate', 0)*100:.2f}%",
                  "vLLM block-level, user-internal (字节级上界)"),
        stat_item("total blocks", f"{s.get('total_blocks', 0):,}",
                  f"empty_prompts={s.get('empty_prompts', 0):,}"),
        stat_item("unique blocks", f"{s.get('unique_blocks', 0):,}",
                  f"占模型 Top-K unique: {share*100:.2f}%"),
        stat_item("hit blocks", f"{s.get('hit_blocks', 0):,}",
                  f"avg blocks/req: {s.get('avg_blocks_per_request', 0):.2f}"),
        stat_item("rpm / unique_rpm",
                  f"{s.get('rpm_avg', 0):g} / {s.get('unique_rpm_avg', 0):g}",
                  "req per minute / unique block per minute"),
    ]
    return f"""
<h2>3. user metrics</h2>
<div class="stat-grid">
  {"".join(items)}
</div>
"""


def render_traffic_timeseries_v3(report: dict, model_report: dict | None) -> str:
    """v3 §4: req/min 时序 (4 user 参考线 + 1 model_p50 绿参考线 + 5 卡片 + spike)."""
    ts = (report.get("time_series") or {}).get("requests_per_minute") or []
    s = report.get("stats") or {}
    earliest = s.get("earliest_timestamp") or 0
    latest = s.get("latest_timestamp") or 0
    if latest >= earliest > 0:
        total_min = (latest // 60) - (earliest // 60) + 1
    else:
        total_min = max(1, len(ts))

    user_q = _padded_quantile([m["count"] for m in ts], total_min)
    model_p50 = (
        (model_report or {}).get("requests_per_min_q", {}).get("p50", 0)
        if model_report else 0
    )
    extra = (
        {"value": model_p50, "label": "model_p50", "color": "#2f855a"}
        if model_p50 > 0 else None
    )

    chart = svg_bar_chart(
        ts, "minute", "count",
        title=f"Requests per minute · avg={user_q['avg']:g} /min",
        x_label="minute (since trace start)",
        y_label="req",
        quantiles=user_q,
        extra_ref_line=extra,
    )

    # Quantile table
    q_table = (
        '<table style="margin-top:8px; font-size:0.9em;">'
        '<tr><th>quantile</th><th>P50</th><th>P80</th><th>P95</th><th>Max</th></tr>'
        f'<tr><td class="label">req/min</td>'
        f'<td>{user_q["p50"]:,}</td><td>{user_q["p80"]:,}</td>'
        f'<td>{user_q["p95"]:,}</td><td>{user_q["max"]:,}</td></tr>'
        '</table>'
    )

    # user-level spike list
    user_spikes = report.get("user_traffic_spikes") or []
    spike_cfg = (model_report or {}).get("spike_config") or {}
    win = spike_cfg.get("window_minutes", 5)
    thr = spike_cfg.get("threshold_multiplier", 5.0)
    if user_spikes:
        rows = "".join(
            f'<tr><td class="label">+{sp["window_start_seconds"]:,}s</td>'
            f'<td>{sp["prev_window_count"]:,}</td>'
            f'<td>→ {sp["this_window_count"]:,}</td>'
            f'<td>×{sp["ratio_to_prev"]}</td></tr>'
            for sp in user_spikes
        )
        spike_block = (
            f'<div class="spike-warning">'
            f'⚠️ <b>{len(user_spikes)} 个 ≥ ×{thr} user 级突变</b> (窗口 = {win} min)'
            f'<table><tr><th>window_start</th><th>prev</th><th>this</th><th>ratio</th></tr>'
            f'{rows}</table></div>'
        )
    else:
        spike_block = (
            f'<div class="no-spike">无 ≥ ×{thr} user 级突变 '
            f'(窗口 = {win} min)。流量稳定。</div>'
        )

    return f"""
<h2>4. 请求量时序</h2>
<div class="ts-block">
  <div class="ts-chart">{chart}</div>
  {_ts_quantile_cards_v3(user_q, "(req/min)")}
  {q_table}
  {spike_block}
</div>
"""


def render_cache_pressure_v3(report: dict, model_report: dict | None) -> str:
    """v3 §5: new_block/s 时序 + cumulative WS + WS 状态."""
    ts = (report.get("time_series") or {}).get("new_unique_blocks_per_second") or []
    cumulative = (report.get("time_series") or {}).get("cumulative_unique_blocks") or []
    s = report.get("stats") or {}
    earliest = s.get("earliest_timestamp") or 0
    latest = s.get("latest_timestamp") or 0
    if latest >= earliest > 0:
        total_sec = latest - earliest + 1
    else:
        total_sec = max(1, len(ts))

    user_q = _padded_quantile([d["count"] for d in ts], total_sec)
    model_p50 = (
        (model_report or {}).get("new_unique_blocks_per_sec_q", {}).get("p50", 0)
        if model_report else 0
    )
    extra = (
        {"value": model_p50, "label": "model_p50", "color": "#2f855a"}
        if model_p50 > 0 else None
    )

    chart = svg_bar_chart(
        ts, "second", "count",
        title=f"New unique blocks per second · avg={user_q['avg']:g} /s",
        x_label="second (since trace start)",
        y_label="blocks",
        quantiles=user_q,
        extra_ref_line=extra,
    )

    q_table = (
        '<table style="margin-top:8px; font-size:0.9em;">'
        '<tr><th>quantile</th><th>P50</th><th>P80</th><th>P95</th><th>Max</th></tr>'
        f'<tr><td class="label">new_block/s</td>'
        f'<td>{user_q["p50"]:,}</td><td>{user_q["p80"]:,}</td>'
        f'<td>{user_q["p95"]:,}</td><td>{user_q["max"]:,}</td></tr>'
        '</table>'
    )

    # Cumulative WS line + state
    cumulative_block = ""
    if cumulative:
        chart_cumul = svg_line_chart(
            cumulative, "second", "total",
            title=f"Cumulative unique blocks · final = {cumulative[-1]['total']:,}",
            x_label="second (since trace start)",
            y_label="unique blocks",
        )
        # WS state
        status, msg = _compute_ws_state_simple(cumulative)
        state_cls = {
            "converged": "ws-converged",
            "not_converged": "ws-not-converged",
            "insufficient": "no-spike",
        }[status]
        cumulative_block = f'''
<div class="ts-chart">{chart_cumul}</div>
<div class="{state_cls}">WS 状态: {msg}</div>
'''

    gb_todo = (
        '<div class="tbd-note">GB 估计 — TODO: 待 model_report.json 补 '
        '<code>kv_bytes_per_token</code> + <code>tokens_per_byte_avg</code>. '
        '详见 <code>metrics_glossary.md §6</code>.</div>'
    )

    return f"""
<h2>5. cache 压力</h2>
<div class="ts-block">
  <div class="ts-chart">{chart}</div>
  {_ts_quantile_cards_v3(user_q, "(blocks/s)")}
  {q_table}
  {gb_todo}
  {cumulative_block}
</div>
"""


def _compute_ws_state_simple(cumulative: list[dict], threshold_pct: float = 5.0,
                              tail_minutes: int = 5) -> tuple[str, str]:
    """Simplified WS state — last tail_minutes slope vs avg slope."""
    if len(cumulative) < 2:
        return ("insufficient", "数据不足")
    pts = sorted(cumulative, key=lambda d: d["second"])
    t_start = pts[0]["second"]
    t_end = pts[-1]["second"]
    total = pts[-1]["total"]
    duration = t_end - t_start
    if duration <= 0:
        return ("insufficient", "trace span = 0")
    avg_slope = total / duration

    tail_secs = tail_minutes * 60
    tail_cut = t_end - tail_secs
    tail_pts = [p for p in pts if p["second"] >= tail_cut]
    if len(tail_pts) < 2:
        return ("insufficient", f"trace span < {tail_minutes} min × 2")
    tail_t = tail_pts[-1]["second"] - tail_pts[0]["second"]
    if tail_t == 0:
        return ("insufficient", "尾部时间窗为 0")
    tail_slope = (tail_pts[-1]["total"] - tail_pts[0]["total"]) / tail_t
    if avg_slope == 0:
        return ("insufficient", "平均斜率 0")
    ratio = tail_slope / avg_slope
    if ratio < threshold_pct / 100.0:
        return ("converged",
                f"✅ <b>已收敛</b> ({ratio*100:.1f}% × avg, &lt; {threshold_pct}%) → "
                f"C 池化容量保障可行 (≥ {total:,} block).")
    return ("not_converged",
            f"⚠️ <b>持续上升</b> ({ratio*100:.1f}% × avg, ≥ {threshold_pct}%) → "
            f"WS 无上限, C 池化收益有限.")


def render_reuse_time(report: dict) -> str:
    """v3 §6: reuse_time CDF + 4 分位数 + LRU 解读."""
    q = report.get("reuse_time_quantiles") or {}
    cdf = report.get("reuse_time_cdf_points") or []
    inter_arrival_p50 = (report.get("inter_arrival_gaps_seconds") or {}).get("p50", 0)

    if not cdf or q.get("count", 0) == 0:
        return """
<h2>6. reuse time (block 维度复用间隔)</h2>
<p class="note">无 reuse_time 数据 (该 user 所有 block 仅出现 1 次, 无复用)。</p>
"""

    chart = svg_cdf_log_x(cdf, q)

    cards = "".join([
        stat_item("avg", f"{q.get('avg', 0):g} s", ""),
        stat_item("p50", f"{q.get('p50', 0):,} s", ""),
        stat_item("p80", f"{q.get('p80', 0):,} s", ""),
        stat_item("p95", f"{q.get('p95', 0):,} s", ""),
        stat_item("max", f"{q.get('max', 0):,} s",
                  f"count = {q.get('count', 0):,}"),
    ])

    # LRU affinity diagnosis
    rt_p50 = q.get("p50", 0)
    if inter_arrival_p50 and rt_p50:
        ratio_label = f"reuse_p50 / inter_arrival_p50 = {rt_p50/inter_arrival_p50:.2f}x"
        if rt_p50 > 2 * inter_arrival_p50:
            diag = (
                '<div class="diagnostic-note">'
                f'⚠️ <b>reuse_time p50 ({rt_p50}s) ≫ inter_arrival p50 ({inter_arrival_p50}s)</b>: '
                f'{ratio_label}. block 复用慢于相邻 request → LRU 可能不够, '
                f'建议 <b>B(2) 多队列 LRU</b> 或 <b>chain pin</b>.'
                '</div>'
            )
        else:
            diag = (
                '<div class="diagnostic-note">'
                f'✅ <b>reuse_time p50 ≈ inter_arrival p50</b> ({ratio_label}): '
                f'复用紧跟请求, LRU 友好, <b>B(1) 默认 LRU 即可</b>.'
                '</div>'
            )
    else:
        diag = ""

    return f"""
<h2>6. reuse time (block 维度复用间隔)</h2>
<div class="ts-block">
  <div class="ts-chart">{chart}</div>
  <div class="stat-grid" style="grid-template-columns:repeat(5,1fr);">{cards}</div>
  {diag}
</div>
"""


def render_lcp_v3(report: dict, forest: dict) -> str:
    """v3 §7: LCP histogram + P30/P50/P80/P95/Max + Top 10 + 反常诊断."""
    lcp_d = report.get("lcp_distribution") or {}
    histogram = lcp_d.get("histogram") or []
    q = lcp_d.get("quantiles") or {}
    top10 = lcp_d.get("top10_lcp_values") or []
    bucket_size = q.get("bucket_size", 1)

    chart = svg_histogram(
        histogram,
        title=f"Per-request LCP histogram (bucket_size={bucket_size})",
    )

    q_table = (
        '<table style="margin-top:8px; font-size:0.9em;">'
        '<tr><th>quantile</th><th>P30</th><th>P50</th><th>P80</th>'
        '<th>P95</th><th>Max</th></tr>'
        f'<tr><td class="label">LCP (block)</td>'
        f'<td>{q.get("p30", 0):,}</td><td>{q.get("p50", 0):,}</td>'
        f'<td>{q.get("p80", 0):,}</td><td>{q.get("p95", 0):,}</td>'
        f'<td>{q.get("max", 0):,}</td></tr>'
        '</table>'
    )

    # Top 10 LCP table
    top_rows = "".join(
        f'<tr><td>{e["lcp_value"]}</td><td>{e["request_count"]:,}</td></tr>'
        for e in top10
    )
    top_table = (
        '<table class="lcp-anomaly-table" style="margin-top:8px;">'
        '<thead><tr><th>LCP 值</th><th>命中 request 数</th></tr></thead>'
        f'<tbody>{top_rows}</tbody></table>'
    )

    # Anomaly diagnosis based on hit_rate × chain_length
    s = report.get("stats") or {}
    hit_rate = s.get("ideal_hit_rate", 0) or 0
    chains = forest.get("chains", []) or []
    chain_count = len(chains)
    dom_len = chains[0]["chain_length"] if chains else 0

    # Top 10 集中度
    top_values = [e["lcp_value"] for e in top10]
    top0_count = sum(e["request_count"] for e in top10 if e["lcp_value"] == 0)
    top_total = sum(e["request_count"] for e in top10)
    pct_at_zero = top0_count / top_total * 100 if top_total else 0

    diag_msg = ""
    if hit_rate >= 0.60 and dom_len > 0 and dom_len < 30 and top0_count < top_total * 0.3:
        diag_msg = (
            f'<b>hit_rate 高 ({hit_rate:.2f}) 但 chain_length 短 ({dom_len} block)</b>: '
            f'Top-10 LCP 分散在非零值上, 暗示<b>长文档复用</b> '
            f'(非 chain 部分的字节共享, chain forest 阈值未捕获)。'
        )
    elif hit_rate <= 0.30 and dom_len >= 30:
        diag_msg = (
            f'<b>hit_rate 低 ({hit_rate:.2f}) 但 chain_length 长 ({dom_len} block)</b>: '
            f'chain 是 <b>death chain</b>, 实际不被走完。可能 system prompt '
            f'已弃用 / 业务 drift。建议 D prompt 重写。'
        )
    elif pct_at_zero >= 50:
        diag_msg = (
            f'<b>Top-10 LCP 集中在 0 ({pct_at_zero:.0f}%)</b>: 大量 request 冷启动, '
            f'chain pin 救不动。考虑 D 业务侧重写。'
        )
    elif chain_count >= 3:
        diag_msg = (
            f'<b>多 chain ({chain_count} 条) + Top-10 LCP 分散</b>: '
            f'多业务 router 模式, 推荐 B(2)/B(3) 多队列。'
        )

    diag_block = (
        f'<div class="diagnostic-note">📊 反常诊断: {diag_msg}</div>'
        if diag_msg else ""
    )

    return f"""
<h2>7. Per-request LCP</h2>
<div class="chart">{chart}
  <div class="chart-caveat">LCP = matched prefix-path-key block count per request.</div>
</div>
{q_table}
<h3 style="margin-top:14px;">Top 10 LCP 值</h3>
{top_table}
{diag_block}
"""


def render_chain_forest_with_shadow(report: dict, forest: dict) -> str:
    """v3 §8: chain forest cards + chain_shadow_pairs warning."""
    chains = forest.get("chains", []) or []
    top_cov = chains[0]["coverage_pct"] if chains else 0.0
    shadow_pairs = report.get("chain_shadow_pairs") or []

    # Chain shadow warning
    shadow_block = ""
    if shadow_pairs:
        rows = "".join(
            f'<li>chain <code>{sp["chain_a"]}</code> ({sp["chain_a_length"]} block) ↔ '
            f'chain <code>{sp["chain_b"]}</code> ({sp["chain_b_length"]} block): '
            f'前 <b>{sp["shared_prefix_blocks"]}</b> block 共享 '
            f'(ratio_a {sp["ratio_a"]*100:.0f}% / ratio_b {sp["ratio_b"]*100:.0f}%)</li>'
            for sp in shadow_pairs
        )
        shadow_block = (
            '<div class="shadow-warning">'
            '⚠️ <b>chain 数量可能虚高</b>:<ul>' + rows + '</ul>'
            '可能原因: system prompt 输入不全 (前 N block 是共享前缀, '
            '后续被 trie 分成不同 branch)。<br>'
            '建议: 人工检查 decoded content, 必要时合并 chain 或调整 '
            '<code>mc_branch_threshold</code> 让 trie 在更早位置合并。'
            '</div>'
        )

    cards = "".join(render_chain_card(c, top_cov) for c in chains)
    if not cards:
        cards = '<p class="note">No chains satisfied the multi-chain thresholds.</p>'

    return f"""
<h2>8. Chain forest</h2>
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
{shadow_block}
{cards}
"""


def _render_encoder_banner(encoder_meta: dict | None) -> str:
    """Step 1.6: top-of-report banner indicating byte-level vs token-level.

    Empty string when encoder_meta is missing (back-compat for old runs).
    """
    if not encoder_meta:
        return ""
    name = encoder_meta.get("name") or "byte_v1"
    block_size = encoder_meta.get("block_size", 128)
    if name == "glm5_token_v1":
        unit = encoder_meta.get("block_unit", "tokens")
        chat_mode = encoder_meta.get("chat_mode", "wrap_user")
        tokenizer = encoder_meta.get("tokenizer_path", "models/glm5_tokenizer")
        return (
            '<div class="encoder-banner-token">'
            f'🔤 <b>Token-Level (GLM-5)</b>: <code>block_size={block_size} {unit}</code>, '
            f'tokenizer = <code>{html.escape(tokenizer)}</code>, '
            f'chat_mode = <code>{html.escape(chat_mode)}</code>, '
            f'hash = <code>sha256_chain_fallback</code> (与 vllm_hash 不 bit-exact 但 deterministic, '
            '不影响 hit_rate, 见 <code>docs/step1_6_token_level_experiment_plan.md §6</code>).'
            '</div>'
        )
    # byte (regression baseline)
    return (
        '<div class="encoder-banner-byte">'
        f'⚙ <b>Byte-Level</b>: <code>block_size={block_size} bytes</code>, sha256 chain. '
        '字节级数字相对 vllm 实际命中率系统性偏高 0-30pp '
        '(详见 <code>docs/metrics_glossary.md §3</code>); '
        '若需精确, 用 <code>--encoder glm5_token</code> 重跑。'
        '</div>'
    )


def render_user_html(
    report: dict, forest: dict,
    total_users: int, total_requests: int,
    model_report: dict | None = None,
) -> str:
    """v3 main: 顶部 banner + §1-§9 连续编号 sections.

    Section 顺序 (per user_report_html_redesign.md §3):
      §1 模型层指标
      §2 user vs model 对比 (水平条)
      §3 user metrics (流量占比 + unique 占比)
      §4 请求量时序 (4 user 参考线 + 1 model_p50 参考线 + 表格 + user spike)
      §5 cache 压力 (同 §4 结构 + cumulative WS + 状态)
      §6 reuse time CDF (对数 x + 4 分位数表)
      §7 Per-request LCP (含 P30/P80 + Top 10 + 反常诊断)
      §8 Chain forest (含 chain_shadow_pairs 警告)
      §9 Step 3 算法推荐 (含 recommended_queue_count)
    """
    uid = report["user_id"]

    single_tenant = total_users == 1
    tenant_tag = (
        '<span class="tenant-tag">single-tenant model</span>' if single_tenant else ""
    )
    single_tenant_note = (
        '<p class="note">This is the only user_id on this model — '
        'per-user metrics equal full-model metrics.</p>' if single_tenant else ""
    )

    # Caveats from JSON
    caveats = report.get("caveats", [])
    caveat_html = "".join(
        f'<div class="caveat">{html.escape(c)}</div>' for c in caveats
    )

    # v3 顶部 banner: 模型级 vs APP 级 chain 算法差异
    algo_banner = (
        '<div class="algo-diff-banner">'
        'ℹ️ <b>算法差异提示</b>: 此 APP 级报告使用 <b>DFS multi-chain</b> '
        '(<code>mc_branch_threshold=0.05</code>), 每 user 可产出多条 chain。'
        '模型级报告 <code>per_user_chains.html</code> 使用 <b>greedy max-child walk</b> '
        '(<code>branch_threshold=0.25</code>), 每 user 仅 1 条。'
        '<b>chain 数 / 长度差异是设计如此</b>。详见 '
        '<code>docs/user_report_html_redesign.md §1</code>.'
        '</div>'
    )

    # Step 1.6: encoder banner (byte vs token-level GLM-5).
    encoder_banner = _render_encoder_banner(report.get("encoder_meta"))

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<title>Per-user report · {html.escape(uid)}</title>
<style>{CSS}</style>
</head><body>
<h1>Per-user research report (v3)
  <span class="subtle">user_id = {html.escape(uid)}</span>{tenant_tag}
</h1>
{single_tenant_note}
{caveat_html}
{encoder_banner}
{algo_banner}

{render_model_section(model_report)}

{render_user_model_compare(report, model_report)}

{render_user_metrics(report, total_requests)}

{render_traffic_timeseries_v3(report, model_report)}

{render_cache_pressure_v3(report, model_report)}

{render_reuse_time(report)}

{render_lcp_v3(report, forest)}

{render_chain_forest_with_shadow(report, forest)}

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
