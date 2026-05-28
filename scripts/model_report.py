#!/usr/bin/env python3
"""给定一个 CSV 数据集, 生成模型级别 .html 报告 (精简版, 4 块信息).

修正后的现网分析报告 (不再是 7 节超大 per-user HTML), 只保留 4 块:
1. **模型级指标**: 理想命中率 (整库 pooled) / 请求数量 / 平均长度 (token) / 请求时间跨度;
2. **APP 级指标表**: 所有 app-id × {请求量, 占总请求量比例, 平均长度(token), 理想请求命中率(该app隔离)};
3. **模型级 reuse time**: CDF 图 (log-x, 内联 SVG) + 表格 P50/P80/P90/P95/MAX;
4. **每个用户的 LCP distribution**: 每个 app-id 一张 LCP 直方图 (ToB 场景一般 ≤ 50 用户).

口径一致性:
- 模型级理想命中率 = 整库 pooled (跨 app 前缀复用都计入, 贴近线上单实例 vLLM);
- APP 级理想命中率 = 该 app 请求**隔离** pooled (只用自己的前缀); 它的 LCP 列表正是第 4 块画的分布,
  即 app_hit_rate = Σ(app LCP) / Σ(app blocks), 第 2 块与第 4 块同源;
- reuse_time = 某 block 距上次访问的时间间隔 (秒), 模型级 pooled, 需按 timestamp 时序计算
  (与 hit_rate 不同, reuse_time 依赖顺序). 定义见 docs/metrics_glossary.md §2.5。

CSV 列: request_id / user_id / raw_prompt / timestamp, 支持中文别名 (请求ID / 租户ID / 请求参数)
        + UTF-8 BOM. app-id 默认匹配 user_id 列, 可用 --app-col 指定。

用法:
    PYTHONPATH=. .venv_glm5/bin/python3 scripts/model_report.py \
        --dir /mnt/esfs/zhangxiyue/GLM-V5/ --csv GLM-V5-0514.csv \
        --encoder glm5_token --tokenizer-path models/glm5_tokenizer --chat-mode wrap_user \
        --output outputs/GLM-V5-0514_model.html
"""
from __future__ import annotations

import argparse
import csv
import html
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from lib.prompt_encoder import build_encoder_from_args  # noqa: E402
from per_user_report_analyzer import lcp_histogram, percentile_int  # noqa: E402
from render_user_report_html import svg_cdf_log_x, svg_histogram  # noqa: E402

csv.field_size_limit(sys.maxsize)

ALIASES = {"请求ID": "request_id", "租户ID": "user_id", "请求参数": "raw_prompt"}


def get_col(row: dict, target: str) -> str | None:
    for k, v in ALIASES.items():
        if v == target and k in row:
            return row[k]
    return row.get(target)


def _resolve_app(row: dict, app_col: str | None) -> str:
    if app_col:
        return row.get(app_col) or ""
    return get_col(row, "user_id") or ""


def _parse_ts(raw: str | None) -> int | None:
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return None


def _reuse_quantiles(reuse_times: list[int]) -> dict:
    """P50/P80/P90/P95/MAX (+avg/count). 加了 P90 (现有 analyzer 没有)."""
    if not reuse_times:
        return {"p50": 0, "p80": 0, "p90": 0, "p95": 0, "max": 0, "avg": 0.0, "count": 0}
    s = sorted(reuse_times)
    return {
        "p50": percentile_int(s, 50),
        "p80": percentile_int(s, 80),
        "p90": percentile_int(s, 90),
        "p95": percentile_int(s, 95),
        "max": s[-1],
        "avg": round(sum(s) / len(s), 2),
        "count": len(s),
    }


def _reuse_cdf_points(reuse_times: list[int], n_points: int = 50) -> list[dict]:
    """Log-spaced CDF points [{t_seconds, cumulative_pct}] (供 svg_cdf_log_x)."""
    if not reuse_times:
        return []
    import math
    s = sorted(reuse_times)
    n = len(s)
    max_rt = s[-1]
    if max_rt <= 1:
        grid = [0, max_rt]
    else:
        log_max = math.log10(max_rt)
        grid = sorted({0} | {
            max(1, int(round(10 ** (i * log_max / (n_points - 1))))) for i in range(n_points)
        })
    pts = []
    for t in grid:
        cnt = sum(1 for rt in s if rt <= t)
        pts.append({"t_seconds": t, "cumulative_pct": round(cnt / n * 100, 3)})
    return pts


def fmt_duration(seconds: int | None) -> str:
    if not seconds or seconds <= 0:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s and not d:
        parts.append(f"{s}s")
    return " ".join(parts) or "0s"


def analyze_model_csv(csv_path: Path, encoder, app_col: str | None = None) -> dict:
    """单 CSV → 模型级 + 每 app 指标 + 模型级 reuse_time. 单遍 (每 prompt 只编码一次)."""
    records: list[tuple[str, int | None, str]] = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            app = _resolve_app(row, app_col)
            ts = _parse_ts(get_col(row, "timestamp"))
            prompt = get_col(row, "raw_prompt") or ""
            records.append((app, ts, prompt))

    # 时序排序 (reuse_time 依赖顺序); 无 ts 的排到末尾, stable
    records.sort(key=lambda r: (r[1] is None, r[1] if r[1] is not None else 0))

    model_seen: set[bytes] = set()
    model_last_seen: dict[bytes, int] = {}
    reuse_times: list[int] = []
    m_hit = m_blocks = m_tokens = m_reqs = 0
    earliest: int | None = None
    latest: int | None = None

    apps: dict[str, dict] = {}

    def _app(a: str) -> dict:
        return apps.setdefault(a, {
            "seen": set(), "hit": 0, "blocks": 0, "tokens": 0, "reqs": 0, "lcps": [],
        })

    for app, ts, prompt in records:
        m_reqs += 1
        a = _app(app)
        a["reqs"] += 1
        if ts is not None:
            if earliest is None or ts < earliest:
                earliest = ts
            if latest is None or ts > latest:
                latest = ts
        if not prompt:
            a["lcps"].append(0)
            continue

        keys, ntok = encoder.encode_with_length(prompt)
        m_tokens += ntok
        m_blocks += len(keys)
        a["tokens"] += ntok
        a["blocks"] += len(keys)

        # 模型级 pooled LCP
        lcp = 0
        for k in keys:
            if k in model_seen:
                lcp += 1
            else:
                break
        m_hit += lcp

        # 模型级 reuse_time (chronological)
        if ts is not None:
            for k in keys:
                prev = model_last_seen.get(k)
                if prev is not None:
                    reuse_times.append(ts - prev)
                model_last_seen[k] = ts
        model_seen.update(keys)

        # APP 级隔离 LCP
        alcp = 0
        for k in keys:
            if k in a["seen"]:
                alcp += 1
            else:
                break
        a["hit"] += alcp
        a["lcps"].append(alcp)
        a["seen"].update(keys)

    span = (latest - earliest) if (earliest is not None and latest is not None) else None

    app_rows = []
    for name, a in apps.items():
        app_rows.append({
            "app_id": name,
            "reqs": a["reqs"],
            "pct": (a["reqs"] / m_reqs * 100) if m_reqs else 0.0,
            "avg_len": (a["tokens"] / a["reqs"]) if a["reqs"] else 0.0,
            "ideal_hit_rate": (a["hit"] / a["blocks"]) if a["blocks"] else 0.0,
            "lcps": a["lcps"],
        })
    app_rows.sort(key=lambda r: -r["reqs"])

    return {
        "model": {
            "ideal_hit_rate": (m_hit / m_blocks) if m_blocks else 0.0,
            "reqs": m_reqs,
            "avg_len": (m_tokens / m_reqs) if m_reqs else 0.0,
            "span_seconds": span,
            "total_blocks": m_blocks,
            "unique_blocks": len(model_seen),
            "n_apps": len(apps),
        },
        "reuse_quantiles": _reuse_quantiles(reuse_times),
        "reuse_cdf": _reuse_cdf_points(reuse_times),
        "apps": app_rows,
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  margin:0;padding:24px 32px;color:#1a202c;background:#f7fafc;}
h1{font-size:22px;margin:0 0 4px;} h2{font-size:16px;margin:30px 0 10px;
  border-left:4px solid #805ad5;padding-left:8px;}
.sub{color:#718096;font-size:13px;margin-bottom:18px;}
.cards{display:flex;gap:14px;flex-wrap:wrap;}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:14px 18px;min-width:150px;}
.card .v{font-size:24px;font-weight:700;color:#553c9a;} .card .k{font-size:12px;color:#718096;margin-top:2px;}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px;}
th,td{border:1px solid #e2e8f0;padding:6px 10px;text-align:right;}
th{background:#edf2f7;color:#2d3748;} td.l,th.l{text-align:left;}
tr:nth-child(even) td{background:#f9fafb;}
.q-table{width:auto;margin-top:10px;} .q-table td,.q-table th{text-align:center;padding:6px 16px;}
.lcp-card{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:10px 12px;margin:10px 0;}
.lcp-card .cap{font-size:13px;color:#2d3748;font-weight:600;margin-bottom:4px;word-break:break-all;}
.lcp-card .meta{font-size:11px;color:#718096;margin-bottom:4px;}
.note{color:#a0aec0;font-size:11px;margin-top:6px;}
"""


def _card(value: str, key: str) -> str:
    return f'<div class="card"><div class="v">{value}</div><div class="k">{html.escape(key)}</div></div>'


def build_html(stats: dict, meta: dict, csv_name: str, len_unit: str) -> str:
    m = stats["model"]
    q = stats["reuse_quantiles"]

    cards = "".join([
        _card(f'{m["ideal_hit_rate"] * 100:.2f}%', "理想命中率 (整库 pooled)"),
        _card(f'{m["reqs"]:,}', "请求数量"),
        _card(f'{m["avg_len"]:,.0f}', f"平均长度 ({len_unit})"),
        _card(fmt_duration(m["span_seconds"]), "请求时间跨度"),
        _card(f'{m["n_apps"]:,}', "APP 数"),
    ])

    # APP 表
    app_rows = "".join(
        f'<tr><td class="l">{html.escape(r["app_id"]) or "(空)"}</td>'
        f'<td>{r["reqs"]:,}</td><td>{r["pct"]:.2f}%</td>'
        f'<td>{r["avg_len"]:,.0f}</td><td>{r["ideal_hit_rate"] * 100:.2f}%</td></tr>'
        for r in stats["apps"]
    )
    app_table = (
        '<table><tr><th class="l">app-id</th><th>请求量</th><th>占比</th>'
        f'<th>平均长度({len_unit})</th><th>理想请求命中率</th></tr>{app_rows}</table>'
    )

    # reuse_time CDF + 分位表
    cdf_svg = svg_cdf_log_x(stats["reuse_cdf"], q)
    q_table = (
        '<table class="q-table"><tr><th>P50</th><th>P80</th><th>P90</th><th>P95</th><th>MAX</th>'
        '<th>avg</th><th>样本数</th></tr>'
        f'<tr><td>{fmt_duration(q["p50"])}</td><td>{fmt_duration(q["p80"])}</td>'
        f'<td>{fmt_duration(q["p90"])}</td><td>{fmt_duration(q["p95"])}</td>'
        f'<td>{fmt_duration(q["max"])}</td><td>{q["avg"]:,.0f}s</td><td>{q["count"]:,}</td></tr></table>'
    )

    # 每用户 LCP distribution
    lcp_cards = []
    for r in stats["apps"]:
        hist, lq, _top = lcp_histogram(r["lcps"])
        svg = svg_histogram(hist, title=f'LCP — {r["app_id"] or "(空)"}'[:60])
        lcp_cards.append(
            f'<div class="lcp-card"><div class="cap">{html.escape(r["app_id"]) or "(空)"}</div>'
            f'<div class="meta">{r["reqs"]:,} reqs · LCP p50={lq["p50"]} p80={lq["p80"]} '
            f'p95={lq["p95"]} max={lq["max"]} (blocks)</div>{svg}</div>'
        )
    lcp_section = "".join(lcp_cards) or '<div class="note">无用户数据</div>'

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>Model report — {html.escape(csv_name)}</title>
<style>{_CSS}</style></head><body>
<h1>模型级理想 KV cache 分析 — {html.escape(csv_name)}</h1>
<div class="sub">encoder: <code>{html.escape(meta['name'])}</code> · block_size={meta['block_size']} {meta['block_unit']}
 · chat_mode={html.escape(str(meta.get('chat_mode')))} · tokenizer=<code>{html.escape(str(meta.get('tokenizer_path')))}</code>
 · 唯一 block {m['unique_blocks']:,} / 总 block {m['total_blocks']:,}</div>

<h2>1. 模型级指标</h2>
<div class="cards">{cards}</div>

<h2>2. APP 级指标 ({m['n_apps']} 个 app-id)</h2>
{app_table}

<h2>3. 模型级 reuse time</h2>
{cdf_svg}
{q_table}
<div class="note">reuse_time = 某 block 距上次访问的时间间隔; 模型级 pooled。
核心算式: cache_pressure_GB/min × reuse_time_P95(分钟) ≈ 维持命中率所需 cache 容量。</div>

<h2>4. 每个用户的 LCP distribution</h2>
{lcp_section}
</body></html>"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", required=True, help="CSV 文件名 (配合 --dir) 或完整路径")
    p.add_argument("--dir", type=Path, default=None, help="数据集文件夹; 给了则读 <dir>/<csv>")
    p.add_argument("--output", type=Path, default=None,
                   help="输出 HTML 路径 (默认 ./<csv名>_model_report.html)")
    p.add_argument("--app-col", type=str, default=None, help="app-id 列名 (默认 user_id/租户ID)")
    p.add_argument("--encoder", type=str, default="glm5_token",
                   choices=["byte", "glm5_token", "hf_token"])
    p.add_argument("--tokenizer-path", type=str, default="models/glm5_tokenizer")
    p.add_argument("--chat-mode", type=str, default="wrap_user",
                   choices=["raw", "wrap_user", "messages"])
    p.add_argument("--block-size", type=int, default=128)
    args = p.parse_args(argv)

    csv_path = (args.dir / args.csv) if args.dir else Path(args.csv)
    if not csv_path.is_file():
        print(f"error: CSV 不存在: {csv_path}", file=sys.stderr)
        return 1

    encoder, meta = build_encoder_from_args(args)
    len_unit = "tokens" if meta["block_unit"] == "tokens" else "bytes"
    print(f"Encoder: {meta['name']} ({meta['block_unit']}, chat_mode={meta['chat_mode']})")
    print(f"分析 {csv_path} ...")
    stats = analyze_model_csv(csv_path, encoder, app_col=args.app_col)
    m = stats["model"]
    print(f"  {m['reqs']:,} reqs, {m['n_apps']} apps, hit_rate={m['ideal_hit_rate']:.4f}, "
          f"reuse样本={stats['reuse_quantiles']['count']:,}")

    out = args.output or Path(f"{csv_path.stem}_model_report.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(stats, meta, csv_path.name, len_unit), encoding="utf-8")
    print(f"HTML → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
