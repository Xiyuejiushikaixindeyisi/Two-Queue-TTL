#!/usr/bin/env python3
"""Render the app-level 4-variant prompt-rewrite HTML report (Stage 3).

Reads output from `per_user_report_4variant.py` and produces a single
self-contained HTML comparing the 4 variants (base / reorder / placeholder /
both) on ideal hit rate, chain count, chain avg length, chain coverage.

Output is offline-safe (no external assets, no JS), matching the rest of the
platform's report style. Per the docs/stage3_prompt_rewrite_plan.md plan,
output is one HTML per analyzer run (= one app), with a per-user breakdown
table inside.

Usage
-----
    python scripts/render_user_report_4variant_html.py \\
        --input-dir outputs/<app>/4variant \\
        --output    outputs/<app>/4variant_report.html \\
        --app-name  <app>
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
from pathlib import Path
from typing import Any

CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  max-width: 1280px; margin: 24px auto; padding: 0 24px; color: #2d3748;
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
.risk-low  { background: #c6f6d5; color: #22543d; padding: 2px 8px;
  border-radius: 3px; font-size: 0.75em; font-weight: 600; }
.risk-mid  { background: #fefcbf; color: #744210; padding: 2px 8px;
  border-radius: 3px; font-size: 0.75em; font-weight: 600; }
table { border-collapse: collapse; margin-top: 10px; font-size: 0.92em;
  width: 100%; }
th, td { padding: 7px 12px; text-align: right; border-bottom: 1px solid #e2e8f0; }
th { background: #edf2f7; text-align: center; font-weight: 600; color: #2d3748; }
th.first, td.first { text-align: left; }
td.metric-name { background: #f7fafc; font-weight: 500; color: #4a5568; }
td.base { color: #4a5568; }
td.variant { background: #fefcfb; }
td.delta-pos { color: #22543d; font-weight: 600; }
td.delta-neg { color: #742a2a; font-weight: 600; }
td.delta-zero { color: #718096; }
.user-section { margin-top: 40px; padding: 12px 18px; background: #f7fafc;
  border-left: 3px solid #4299e1; border-radius: 3px; }
.user-section h3 { margin-top: 0; }
"""


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _fmt_pp(x: float) -> str:
    sign = "+" if x > 0 else ""
    return f"{sign}{x * 100:.2f} pp"


def _fmt_num(x: float, decimals: int = 2) -> str:
    sign = "+" if x > 0 else ""
    return f"{sign}{x:.{decimals}f}"


def _delta_class(delta: float, higher_is_better: bool = True) -> str:
    if abs(delta) < 1e-9:
        return "delta-zero"
    good = (delta > 0) if higher_is_better else (delta < 0)
    return "delta-pos" if good else "delta-neg"


_RISK_LABEL = {
    "base":        ("", ""),
    "reorder":     ("低",  "risk-low"),
    "placeholder": ("中*", "risk-mid"),
    "both":        ("中*", "risk-mid"),
}


def _render_summary_table(summary: dict) -> str:
    """Top-level table: each row = a variant; columns = aggregated metrics."""
    variants = summary["variants"]
    users = summary["users"]

    # Aggregate metrics: weighted average by request count.
    aggregates: dict[str, dict[str, float]] = {}
    total_reqs = sum(u["request_count"] for u in users) or 1
    for v in variants:
        hr = sum(u[f"hit_rate_{v}"] * u["request_count"] for u in users) / total_reqs
        cc = sum(u[f"chain_count_{v}"] * u["request_count"] for u in users) / total_reqs
        aggregates[v] = {"hit_rate": hr, "chain_count": cc}

    base = aggregates.get("base", {"hit_rate": 0.0, "chain_count": 0.0})

    rows = []
    rows.append('<table>')
    rows.append('<thead><tr>'
                '<th class="first">变体</th>'
                '<th>理想命中率</th>'
                '<th>Δ vs base</th>'
                '<th>chain 数 (加权均值)</th>'
                '<th>Δ vs base</th>'
                '<th>精度风险</th>'
                '</tr></thead><tbody>')
    for v in variants:
        m = aggregates[v]
        d_hr = m["hit_rate"] - base["hit_rate"]
        d_cc = m["chain_count"] - base["chain_count"]
        risk_text, risk_cls = _RISK_LABEL.get(v, ("", ""))
        risk_html = (
            f'<span class="{risk_cls}">{html.escape(risk_text)}</span>'
            if risk_cls else "—"
        )
        rows.append(
            f'<tr>'
            f'<td class="first metric-name">{html.escape(v)}</td>'
            f'<td>{_fmt_pct(m["hit_rate"])}</td>'
            f'<td class="{_delta_class(d_hr, higher_is_better=True)}">'
            f'  {_fmt_pp(d_hr) if v != "base" else "—"}</td>'
            f'<td>{m["chain_count"]:.2f}</td>'
            f'<td class="{_delta_class(d_cc, higher_is_better=False)}">'
            f'  {_fmt_num(d_cc, 2) if v != "base" else "—"}</td>'
            f'<td>{risk_html}</td>'
            f'</tr>'
        )
    rows.append('</tbody></table>')
    return "\n".join(rows)


def _render_per_user_section(user_id: str, user_detail: dict) -> str:
    """One section per user with the 4-variant breakdown."""
    variants = user_detail["variants_analyzed"]
    per = user_detail["variants"]
    deltas = user_detail.get("deltas_vs_base", {})

    rows = []
    rows.append('<div class="user-section">')
    rows.append(f'<h3>{html.escape(str(user_id))} <span style="color:#718096;font-weight:normal;font-size:0.8em;">'
                f'({user_detail["request_count"]} 请求)</span></h3>')

    # 4-variant table: rows = metrics, columns = variants
    rows.append('<table>')
    cols_header = ' '.join(
        f'<th>{html.escape(v)}</th>' for v in variants
    )
    rows.append(f'<thead><tr><th class="first">指标</th>{cols_header}</tr></thead><tbody>')

    metrics = [
        ("理想命中率",   "ideal_hit_rate",    True,  _fmt_pct),
        ("chain 数",     "chain_count",       False, lambda x: f"{x}"),
        ("chain avg 长度", "chain_avg_length", True, lambda x: f"{x:.2f}"),
        ("chain 覆盖率", "chain_coverage",    True,  _fmt_pct),
    ]
    for label, key, higher_better, fmt in metrics:
        cells = []
        for v in variants:
            val = per[v].get(key, 0)
            cells.append(f'<td class="variant">{fmt(val)}</td>')
        rows.append(
            f'<tr>'
            f'<td class="first metric-name">{html.escape(label)}</td>'
            f'{"".join(cells)}'
            f'</tr>'
        )

    # Delta row vs base
    delta_metrics = [
        ("Δ 命中率 (pp)", "ideal_hit_rate_delta", True, _fmt_pp),
        ("Δ chain 数",     "chain_count_delta",   False, lambda x: f"{x:+.0f}"),
    ]
    for label, key, higher_better, fmt in delta_metrics:
        cells = []
        for v in variants:
            if v == "base":
                cells.append('<td class="base">—</td>')
            else:
                d = deltas.get(v, {}).get(key, 0)
                cls = _delta_class(d, higher_is_better=higher_better)
                cells.append(f'<td class="{cls}">{fmt(d)}</td>')
        rows.append(
            f'<tr>'
            f'<td class="first metric-name">{html.escape(label)}</td>'
            f'{"".join(cells)}'
            f'</tr>'
        )

    rows.append('</tbody></table>')
    rows.append('</div>')
    return "\n".join(rows)


def _load_user_details(input_dir: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in input_dir.glob("*_4variant.json"):
        if path.name == "summary_4variant.json":
            continue
        with open(path, encoding="utf-8") as f:
            detail = json.load(f)
        out[detail["user_id"]] = detail
    return out


def render_html(summary: dict, user_details: dict[str, dict], app_name: str) -> str:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append("<html><head>")
    parts.append('<meta charset="utf-8">')
    parts.append(f"<title>4-variant prompt-rewrite report — {html.escape(app_name)}</title>")
    parts.append(f"<style>{CSS}</style>")
    parts.append("</head><body>")

    parts.append(
        f'<h1>Prompt-Rewrite 4 变体对比 · {html.escape(app_name)}'
        f'<span class="subtle"> · {timestamp}</span></h1>'
    )

    parts.append('<div class="meta">')
    parts.append(f"trace: {html.escape(summary['trace'])}<br>")
    parts.append(f"variants: {', '.join(html.escape(v) for v in summary['variants'])}")
    parts.append('</div>')

    parts.append('<div class="caveat">')
    parts.append(
        "<strong>免责说明</strong>: 本报告为离线反事实分析。reorder 变体不修改 "
        "tool 内容, 精度风险低; placeholder/both 变体把 description 里的路径/日期 "
        "替换为占位符 (如 <code>__PATH_0001__</code>), 工具调用准确率<strong>需"
        "真机 A/B 验证</strong>。messages 内容在 4 变体中始终不变, transform 仅"
        "作用于 <code>body[\"tools\"]</code> 字段。"
    )
    parts.append('</div>')

    parts.append("<h2>1. 总览 (加权聚合)</h2>")
    parts.append(_render_summary_table(summary))
    parts.append(
        '<p style="font-size:0.85em;color:#718096;">'
        "加权 = 每个 user 的指标按 request_count 加权; 命中率单位为 pp (percentage point);"
        " chain 数为加权均值, 负 delta 越大越好。"
        "</p>"
    )

    parts.append("<h2>2. Per-user 拆解</h2>")
    # Sort by request_count desc
    users = sorted(summary["users"], key=lambda u: -u["request_count"])
    for u in users:
        detail = user_details.get(u["user_id"])
        if detail is None:
            continue
        parts.append(_render_per_user_section(u["user_id"], detail))

    parts.append("<h2>3. 方法学</h2>")
    parts.append(
        "<ul>"
        "<li>Block size: 128 tokens (vllm-ascend 对齐)</li>"
        "<li>Hash: SHA-256 chain 截 16 hex 字符</li>"
        "<li>Tokenizer: GLM-V5 (vendored, models/glm5_tokenizer)</li>"
        "<li>Chat template: <code>apply_chat_template(messages, tools)</code>"
        " — tools 字段被 4 变体各自 transform</li>"
        "<li>chain 数 / 长度 / 覆盖率定义详见"
        " <a href=\"../../../docs/metrics_glossary.md\">docs/metrics_glossary.md</a></li>"
        "<li>实现细节: <a href=\"../../../docs/stage3_prompt_rewrite_plan.md\">"
        "docs/stage3_prompt_rewrite_plan.md</a></li>"
        "</ul>"
    )

    parts.append("</body></html>")
    return "\n".join(parts)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Render the app-level 4-variant prompt-rewrite HTML report",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--input-dir", required=True,
                   help="Directory produced by per_user_report_4variant.py")
    p.add_argument("--output", required=True, help="Output HTML path")
    p.add_argument("--app-name", default="<app>",
                   help="App identifier shown in the HTML title")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    input_dir = Path(args.input_dir)
    if not input_dir.exists():
        raise SystemExit(f"Input directory does not exist: {input_dir}")

    summary_path = input_dir / "summary_4variant.json"
    if not summary_path.exists():
        raise SystemExit(f"Missing {summary_path}; run per_user_report_4variant.py first")

    with open(summary_path, encoding="utf-8") as f:
        summary: dict[str, Any] = json.load(f)

    user_details = _load_user_details(input_dir)

    html_text = render_html(summary, user_details, args.app_name)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_text, encoding="utf-8")
    print(f"Wrote {out_path} ({len(html_text):,} chars)")


if __name__ == "__main__":
    main()
