"""Self-contained inline-SVG charts (offline, no JS / matplotlib / 中文字体).

抽自 scripts/render_user_report_html.py, 供 model_report / app_report 共用, 让新报告
不再为了两个画图函数去 import 笨重的 legacy 渲染器。生成的是内联 SVG 字符串, 直接嵌进
单文件 HTML, 离线可看。
"""
from __future__ import annotations

import html
import math


def svg_cdf_log_x(cdf_points: list[dict], quantiles: dict,
                  width: int = 900, height: int = 220) -> str:
    """reuse_time CDF on log-x axis with p50/p80/p95 markers.

    cdf_points: [{t_seconds, cumulative_pct}]; quantiles: {p50,p80,p95,...}。
    """
    if not cdf_points:
        return '<svg class="chart-empty">empty: reuse_time CDF</svg>'

    pad_l, pad_r, pad_t, pad_b = 70, 80, 28, 32
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

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

    y_ticks = []
    for pct in (0, 50, 100):
        y = pad_t + plot_h - pct / 100.0 * plot_h
        y_ticks.append(
            f'<line x1="{pad_l}" y1="{y}" x2="{pad_l + plot_w}" y2="{y}" '
            f'stroke="#e2e8f0" stroke-width="1"/>'
            f'<text x="{pad_l - 6}" y="{y + 3}" text-anchor="end" '
            f'font-size="10" fill="#718096">{pct}%</text>'
        )

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
