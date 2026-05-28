#!/usr/bin/env python3
"""给定原始数据 + app-id, 生成 APP 级别 .html 报告 (一条命令, 内部完成 convert + 分析 + 渲染).

两种输入:
- **CSV**: `--csv <生产trace.csv> --app-id <租户ID>`. 每行 `请求参数` 是 {"tools":[...],
  "messages":[...]} JSON; 内部对该 app 的每条请求渲染 4 变体 (base/reorder/placeholder/both)
  → 出全部 5 块。
- **txt 文件夹**: `--txt-dir <folder>`. 每个 .txt = 一条请求的纯 prompt 文本 (无 tools)。
  整个文件夹视作一个 app; 无 tools 可重排/占位 → **省略第 4 块 (4 变体)**, 其余 4 块照出。

报告 5 块:
1. APP 指标: 理想命中率 / 请求数量 / 平均长度 (token) / 请求时间跨度;
2. APP reuse time: CDF 图 (log-x) + 表 P50/P80/P90/P95/MAX;
3. per-request LCP distribution: 柱状图 + TOP10 表 (LCP 长度 + 覆盖率=该 LCP 值的请求占比);
4. 4 变体表 (仅 CSV): 行=指标 (理想命中率/chain数量/请求占比最多chain的长度/该chain请求占比), 列=base/reorder/placeholder/both;
5. chain forest (base): base 变体的显著 chain 列表 (长度 + 请求数 + 请求占比)。

口径: 理想命中率/LCP/chain 全部基于 base 变体 token 级 block; reuse_time = block 距上次访问间隔,
按 timestamp 时序计算 (见 docs/metrics_glossary.md §2.5); 平均长度 = apply_chat_template 后 token 数。

用法:
    PYTHONPATH=. .venv_glm5/bin/python3 scripts/app_report.py \
        --csv /mnt/esfs/zhangxiyue/GLM-V5/GLM-V5-0514.csv --app-id S0012345 \
        --tokenizer-path models/glm5_tokenizer --output outputs/app_S0012345.html
    PYTHONPATH=. .venv_glm5/bin/python3 scripts/app_report.py \
        --txt-dir /mnt/esfs/zhangxiyue/qwen_txt/ \
        --encoder hf_token --tokenizer-path models/qwen_v3_tokenizer --output outputs/app_qwen.html
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from lib.prompt_encoder import build_encoder_from_args  # noqa: E402
from lib.prompt_rewrite import DEFAULT_PATTERNS, apply_variant, load_patterns_config  # noqa: E402
from lib.prompt_rewrite.chat_render import render_to_tokens  # noqa: E402
from multi_chain_finder import (  # noqa: E402
    DEFAULT_MAX_CHAINS,
    DEFAULT_MC_BRANCH_THRESHOLD,
    DEFAULT_MC_COVERAGE_THRESHOLD,
    DEFAULT_MIN_CHAIN_COVERAGE,
    DEFAULT_MIN_CHAIN_LENGTH,
    find_chain_forest,
)
from per_user_report_4variant import VARIANTS, _build_trie, _count_trie_hits  # noqa: E402
from per_user_report_analyzer import lcp_histogram  # noqa: E402
from model_report import _reuse_cdf_points, _reuse_quantiles, fmt_duration  # noqa: E402
from render_user_report_html import svg_cdf_log_x, svg_histogram  # noqa: E402

csv.field_size_limit(sys.maxsize)

ALIASES = {"请求ID": "request_id", "租户ID": "user_id", "请求参数": "raw_prompt"}
_REQUEST_INPUT_ALIASES = ("raw_prompt", "request_input", "请求参数", "request_params")


def _norm_keys(row: dict) -> dict:
    return {(k or "").lstrip("﻿").strip(): v for k, v in row.items()}


def get_col(row: dict, target: str) -> str | None:
    for k, v in ALIASES.items():
        if v == target and k in row:
            return row[k]
    return row.get(target)


def _first_present(row: dict, keys) -> str | None:
    for k in keys:
        if row.get(k):
            return row[k]
    return None


def _resolve_app(row: dict, app_col: str | None) -> str:
    if app_col:
        return row.get(app_col) or ""
    return get_col(row, "user_id") or ""


def _parse_ts(raw) -> int | None:
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return None


def _chain_bytes(token_ids: list[int], block_size: int) -> list[bytes]:
    """token_ids → SHA-256 chain block keys (bytes). 与 HFTokenEncoder.encode 同算法。"""
    keys: list[bytes] = []
    prev = b""
    for i in range(0, len(token_ids), block_size):
        block = token_ids[i:i + block_size]
        payload = ",".join(str(t) for t in block).encode("utf-8")
        h = hashlib.sha256()
        h.update(prev)
        h.update(payload)
        prev = h.digest()
        keys.append(prev)
    return keys


# ---------------------------------------------------------------------------
# Build records (records = [{request_id, ts, input_length, hash_ids:{variant:[bytes]}}])
# ---------------------------------------------------------------------------

def build_records_from_csv(csv_path: Path, app_id: str, tokenizer, patterns,
                           variants, block_size: int, app_col: str | None) -> tuple[list[dict], dict]:
    records: list[dict] = []
    counts = {"matched": 0, "no_json": 0, "no_messages": 0, "render_error": 0}
    first_err = ""
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for i, raw in enumerate(csv.DictReader(f)):
            row = _norm_keys(raw)
            if _resolve_app(row, app_col) != app_id:
                continue
            counts["matched"] += 1
            ri = _first_present(row, _REQUEST_INPUT_ALIASES)
            if not ri:
                counts["no_json"] += 1
                continue
            try:
                body = json.loads(ri)
            except (json.JSONDecodeError, TypeError):
                counts["no_json"] += 1
                continue
            tools = body.get("tools") or []
            messages = body.get("messages") or []
            if not messages:
                counts["no_messages"] += 1
                continue
            try:
                hash_ids: dict[str, list[bytes]] = {}
                base_tokens = 0
                for v in variants:
                    tt = apply_variant(v, tools, patterns=patterns)
                    token_ids = render_to_tokens(tokenizer, messages, tt)
                    hash_ids[v] = _chain_bytes(token_ids, block_size)
                    if v == "base":
                        base_tokens = len(token_ids)
            except Exception as e:  # noqa: BLE001
                counts["render_error"] += 1
                if not first_err:
                    first_err = f"{type(e).__name__}: {e}"
                continue
            records.append({
                "request_id": str(i),
                "ts": _parse_ts(get_col(row, "timestamp")),
                "input_length": base_tokens,
                "hash_ids": hash_ids,
            })
    counts["first_render_error"] = first_err
    return records, counts


def build_records_from_txt(txt_dir: Path, encoder, block_size: int) -> list[dict]:
    """整个文件夹视作一个 app; 每个 .txt 一条请求 (base 变体 only, 无 tools)。"""
    records: list[dict] = []
    for i, txt in enumerate(sorted(txt_dir.rglob("*.txt"))):
        prompt = txt.read_text(encoding="utf-8", errors="replace")
        keys, ntok = encoder.encode_with_length(prompt)
        records.append({
            "request_id": str(i),
            "ts": None,                       # txt 无 timestamp → reuse_time 退化
            "input_length": ntok,
            "hash_ids": {"base": keys},
        })
    return records


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_base(records: list[dict]) -> dict:
    """§1/§2/§3: base 变体顺序扫描 (reuse_time 需按 ts 时序)."""
    ordered = sorted(records, key=lambda r: (r["ts"] is None, r["ts"] if r["ts"] is not None else 0))
    seen: set[bytes] = set()
    last_seen: dict[bytes, int] = {}
    reuse_times: list[int] = []
    lcps: list[int] = []
    hit = total = tokens = 0
    earliest = latest = None
    for r in ordered:
        keys = r["hash_ids"].get("base", [])
        tokens += r["input_length"]
        ts = r["ts"]
        if ts is not None:
            earliest = ts if earliest is None else min(earliest, ts)
            latest = ts if latest is None else max(latest, ts)
        if not keys:
            lcps.append(0)
            continue
        total += len(keys)
        lcp = 0
        for k in keys:
            if k in seen:
                lcp += 1
            else:
                break
        hit += lcp
        lcps.append(lcp)
        if ts is not None:
            for k in keys:
                prev = last_seen.get(k)
                if prev is not None:
                    reuse_times.append(ts - prev)
                last_seen[k] = ts
        seen.update(keys)
    reqs = len(records)
    span = (latest - earliest) if (earliest is not None and latest is not None) else None
    return {
        "reqs": reqs,
        "ideal_hit_rate": (hit / total) if total else 0.0,
        "avg_len": (tokens / reqs) if reqs else 0.0,
        "span_seconds": span,
        "total_blocks": total,
        "unique_blocks": len(seen),
        "lcps": lcps,
        "reuse_quantiles": _reuse_quantiles(reuse_times),
        "reuse_cdf": _reuse_cdf_points(reuse_times),
    }


def variant_metrics(records: list[dict], variant: str, chain_kwargs: dict) -> dict:
    """§4: 单变体 {理想命中率, chain数量, 请求占比最多chain的长度, 该chain请求占比}."""
    root, n_req, n_blocks = _build_trie(records, variant)
    if n_req == 0 or n_blocks == 0:
        return {"ideal_hit_rate": 0.0, "chain_count": 0, "top_chain_length": 0, "top_chain_coverage": 0.0}
    hits = _count_trie_hits(root)
    forest = find_chain_forest(root, **chain_kwargs)
    chains = forest.get("chains", [])
    if chains:
        top = max(chains, key=lambda c: c["coverage_count"])
        top_len = top["chain_length"]
        top_cov = top["coverage_count"] / n_req
    else:
        top_len, top_cov = 0, 0.0
    return {
        "ideal_hit_rate": hits / n_blocks,
        "chain_count": len(chains),
        "top_chain_length": top_len,
        "top_chain_coverage": top_cov,
    }


def base_forest(records: list[dict], chain_kwargs: dict) -> tuple[dict, int]:
    """§5: base 变体 chain forest."""
    root, n_req, _ = _build_trie(records, "base")
    if n_req == 0:
        return {"chains": []}, 0
    return find_chain_forest(root, **chain_kwargs), n_req


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  margin:0;padding:24px 32px;color:#1a202c;background:#f7fafc;}
h1{font-size:21px;margin:0 0 4px;} h2{font-size:16px;margin:28px 0 10px;
  border-left:4px solid #3182ce;padding-left:8px;}
.sub{color:#718096;font-size:13px;margin-bottom:16px;}
.cards{display:flex;gap:14px;flex-wrap:wrap;}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:14px 18px;min-width:140px;}
.card .v{font-size:23px;font-weight:700;color:#2b6cb0;} .card .k{font-size:12px;color:#718096;margin-top:2px;}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px;}
th,td{border:1px solid #e2e8f0;padding:6px 10px;text-align:right;}
th{background:#edf2f7;color:#2d3748;} td.l,th.l{text-align:left;}
tr:nth-child(even) td{background:#f9fafb;}
.q-table,.v-table{width:auto;} .q-table td,.q-table th,.v-table td,.v-table th{text-align:center;padding:6px 14px;}
.note{color:#a0aec0;font-size:11px;margin-top:6px;}
"""


def _card(v: str, k: str) -> str:
    return f'<div class="card"><div class="v">{v}</div><div class="k">{html.escape(k)}</div></div>'


def section_metrics(b: dict, len_unit: str) -> str:
    cards = "".join([
        _card(f'{b["ideal_hit_rate"] * 100:.2f}%', "理想命中率"),
        _card(f'{b["reqs"]:,}', "请求数量"),
        _card(f'{b["avg_len"]:,.0f}', f"平均长度 ({len_unit})"),
        _card(fmt_duration(b["span_seconds"]), "请求时间跨度"),
    ])
    return f'<h2>1. APP 指标</h2><div class="cards">{cards}</div>'


def section_reuse(b: dict) -> str:
    q = b["reuse_quantiles"]
    cdf = svg_cdf_log_x(b["reuse_cdf"], q)
    tbl = (
        '<table class="q-table"><tr><th>P50</th><th>P80</th><th>P90</th><th>P95</th><th>MAX</th>'
        '<th>avg</th><th>样本数</th></tr><tr>'
        f'<td>{fmt_duration(q["p50"])}</td><td>{fmt_duration(q["p80"])}</td>'
        f'<td>{fmt_duration(q["p90"])}</td><td>{fmt_duration(q["p95"])}</td>'
        f'<td>{fmt_duration(q["max"])}</td><td>{q["avg"]:,.0f}s</td><td>{q["count"]:,}</td></tr></table>'
    )
    return (f'<h2>2. APP reuse time</h2>{cdf}{tbl}'
            '<div class="note">reuse_time = 某 block 距上次访问的时间间隔 (秒)。'
            'cache_pressure_GB/min × P95(分钟) ≈ 维持命中率所需 cache 容量。</div>')


def section_lcp(b: dict) -> str:
    hist, q, top10 = lcp_histogram(b["lcps"])
    svg = svg_histogram(hist, title="per-request LCP distribution")
    reqs = b["reqs"] or 1
    rows = "".join(
        f'<tr><td>{t["lcp_value"]:,}</td><td>{t["request_count"]:,}</td>'
        f'<td>{t["request_count"] / reqs * 100:.2f}%</td></tr>'
        for t in top10
    )
    tbl = ('<table class="q-table" style="margin-top:8px"><tr><th>LCP 长度 (blocks)</th>'
           f'<th>请求数</th><th>覆盖率 (请求占比)</th></tr>{rows}</table>')
    return (f'<h2>3. per-request LCP distribution</h2>{svg}{tbl}'
            f'<div class="note">LCP p50={q["p50"]} p80={q["p80"]} p95={q["p95"]} max={q["max"]} (blocks)。'
            '覆盖率 = 该 LCP 值的请求数 / APP 总请求数。</div>')


_V_LABELS = {"base": "base", "reorder": "reorder", "placeholder": "placeholder", "both": "both"}
_M_ROWS = [
    ("理想命中率", "ideal_hit_rate", lambda x: f"{x * 100:.2f}%"),
    ("chain 数量", "chain_count", lambda x: f"{x:,}"),
    ("请求占比最多 chain 的长度 (blocks)", "top_chain_length", lambda x: f"{x:,}"),
    ("该 chain 请求占比", "top_chain_coverage", lambda x: f"{x * 100:.2f}%"),
]


def section_4variant(per_variant: dict, variants: list[str]) -> str:
    head = "".join(f"<th>{_V_LABELS.get(v, v)}</th>" for v in variants)
    rows = ""
    for label, key, fmt in _M_ROWS:
        cells = "".join(f"<td>{fmt(per_variant[v][key])}</td>" for v in variants)
        rows += f'<tr><td class="l">{label}</td>{cells}</tr>'
    return (f'<h2>4. 4 变体对比 (tool reorder / placeholder)</h2>'
            f'<table class="v-table"><tr><th class="l">指标</th>{head}</tr>{rows}</table>'
            '<div class="note">base=原样; reorder=静态 tools 排序前置; placeholder=路径/日期/UUID 占位符化; both=两者。'
            'placeholder 是占位符近似上界, 真实收益需真机 A/B。</div>')


def section_forest(forest: dict, n_req: int) -> str:
    chains = forest.get("chains", [])
    if not chains:
        return '<h2>5. chain forest (base)</h2><div class="note">无显著 chain。</div>'
    rows = ""
    for c in chains:
        share = (c["coverage_count"] / n_req * 100) if n_req else 0.0
        rows += (f'<tr><td>{c["chain_id"]}</td><td>{c["chain_length"]:,}</td>'
                 f'<td>{c["coverage_count"]:,}</td><td>{share:.2f}%</td>'
                 f'<td>{c.get("max_prefix_coverage_pct", 0):.1f}%</td></tr>')
    return (f'<h2>5. chain forest (base)</h2>'
            '<table><tr><th>chain_id</th><th>长度 (blocks)</th><th>请求数</th>'
            f'<th>请求占比</th><th>max 前缀覆盖</th></tr>{rows}</table>'
            '<div class="note">base 变体的显著共享前缀链 (≥ 阈值)。请求占比 = 完整走完该 chain 的请求 / APP 总请求。</div>')


def build_html(app_id: str, b: dict, meta: dict, len_unit: str,
               per_variant: dict | None, forest: dict, n_req: int, source: str) -> str:
    sections = [section_metrics(b, len_unit), section_reuse(b), section_lcp(b)]
    if per_variant is not None:
        sections.append(section_4variant(per_variant, list(per_variant.keys())))
    sections.append(section_forest(forest, n_req))
    body = "\n".join(sections)
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>APP report — {html.escape(app_id)}</title><style>{_CSS}</style></head><body>
<h1>APP 级理想 KV cache 分析 — {html.escape(app_id)}</h1>
<div class="sub">source: <code>{html.escape(source)}</code> · encoder: <code>{html.escape(meta['name'])}</code>
 · block_size={meta['block_size']} {meta['block_unit']} · 唯一 block {b['unique_blocks']:,} / 总 block {b['total_blocks']:,}
 {'· 无 tools → 省略 4 变体块' if per_variant is None else ''}</div>
{body}
</body></html>"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="生产 trace CSV (配合 --app-id)")
    src.add_argument("--txt-dir", type=Path, help="txt 文件夹 (整个文件夹 = 一个 app)")
    p.add_argument("--dir", type=Path, default=None, help="CSV 所在文件夹 (给了则读 <dir>/<csv>)")
    p.add_argument("--app-id", default=None, help="CSV 模式必填: 目标租户ID; txt 模式可选 (默认文件夹名)")
    p.add_argument("--app-col", default=None, help="app-id 列名 (默认 user_id/租户ID)")
    p.add_argument("--output", type=Path, default=None, help="输出 HTML (默认 ./<app>_app_report.html)")
    p.add_argument("--patterns", default=None, help="placeholder patterns JSON (可选)")
    p.add_argument("--encoder", default="glm5_token", choices=["byte", "glm5_token", "hf_token"])
    p.add_argument("--tokenizer-path", default="models/glm5_tokenizer")
    p.add_argument("--chat-mode", default="wrap_user", choices=["raw", "wrap_user", "messages"])
    p.add_argument("--block-size", type=int, default=128)
    p.add_argument("--mc-branch-thr", type=float, default=DEFAULT_MC_BRANCH_THRESHOLD)
    p.add_argument("--mc-cov-thr", type=float, default=DEFAULT_MC_COVERAGE_THRESHOLD)
    p.add_argument("--mc-min-len", type=int, default=DEFAULT_MIN_CHAIN_LENGTH)
    p.add_argument("--mc-min-cov", type=float, default=DEFAULT_MIN_CHAIN_COVERAGE)
    p.add_argument("--mc-max-chains", type=int, default=DEFAULT_MAX_CHAINS)
    args = p.parse_args(argv)

    chain_kwargs = {
        "mc_branch_thr": args.mc_branch_thr, "mc_cov_thr": args.mc_cov_thr,
        "min_chain_length": args.mc_min_len, "min_chain_coverage": args.mc_min_cov,
        "max_chains": args.mc_max_chains,
    }
    encoder, meta = build_encoder_from_args(args)
    len_unit = "tokens" if meta["block_unit"] == "tokens" else "bytes"
    patterns = load_patterns_config(args.patterns) if args.patterns else DEFAULT_PATTERNS

    per_variant: dict | None = None
    if args.csv:
        if not args.app_id:
            print("error: CSV 模式必须给 --app-id", file=sys.stderr)
            return 2
        if not hasattr(encoder, "tokenizer"):
            print("error: CSV 4 变体需 token 编码器 (glm5_token / hf_token), 不能用 byte", file=sys.stderr)
            return 2
        csv_path = (args.dir / args.csv) if args.dir else Path(args.csv)
        if not csv_path.is_file():
            print(f"error: CSV 不存在: {csv_path}", file=sys.stderr)
            return 1
        app_id = args.app_id
        source = str(csv_path)
        print(f"分析 CSV {csv_path}  app-id={app_id} ...")
        records, counts = build_records_from_csv(
            csv_path, app_id, encoder.tokenizer, patterns, VARIANTS, args.block_size, args.app_col)
        print(f"  matched {counts['matched']:,} 行; 成功 {len(records):,}; "
              f"skip no-json {counts['no_json']:,} / no-msg {counts['no_messages']:,} / "
              f"render-err {counts['render_error']:,}")
        if counts["first_render_error"]:
            print(f"  first render error: {counts['first_render_error'][:200]}")
        if not records:
            print("error: 该 app-id 无可分析请求", file=sys.stderr)
            return 1
        per_variant = {v: variant_metrics(records, v, chain_kwargs) for v in VARIANTS}
    else:
        txt_dir = args.txt_dir
        if not txt_dir.is_dir():
            print(f"error: txt 文件夹不存在: {txt_dir}", file=sys.stderr)
            return 1
        app_id = args.app_id or txt_dir.name
        source = str(txt_dir)
        print(f"分析 txt {txt_dir}  (整个文件夹 = app {app_id}) ...")
        records = build_records_from_txt(txt_dir, encoder, args.block_size)
        print(f"  {len(records):,} 个 txt 请求 (无 tools → 省略 4 变体块)")
        if not records:
            print("error: 文件夹里没有 .txt", file=sys.stderr)
            return 1

    b = analyze_base(records)
    forest, n_req = base_forest(records, chain_kwargs)
    print(f"  hit_rate={b['ideal_hit_rate']:.4f}, reqs={b['reqs']:,}, "
          f"reuse样本={b['reuse_quantiles']['count']:,}, chains={len(forest.get('chains', []))}")

    out = args.output or Path(f"{app_id}_app_report.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(app_id, b, meta, len_unit, per_variant, forest, n_req, source),
                   encoding="utf-8")
    print(f"HTML → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
