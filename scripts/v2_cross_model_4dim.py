#!/usr/bin/env python3
"""跨模型 4 维度汇总: 用 v2 工具 outputs 直接产出 4 张排序表 + 算法建议.

4 个维度:
  1. user-level KV cache 命中率排序 (hit_rate desc)
     → 优化空间大的场景候选 (A/B/C/D 子类型推荐)
  2. user-level cache 压力排序 (new_block/s P80 + total unique_blocks)
     → C(1) 池化候选 (压力大 + hit_rate 高)
  3. 模型级复用偏斜 (reuse_inversion_ratio desc)
     → A(1) isolation routing 候选
  4. 用户流量突变 (user_traffic_spikes count / max_ratio desc)
     → 动态扩缩容候选

输入:
  outputs/<model>/per_user_reports/
    ├── model_report.json
    ├── user_summary.csv
    └── <user_dir>/
        ├── user_report.json
        └── chain_forest.json

输出:
  outputs/v2_4dim_summary.csv  (所有维度合并 flat csv, 一行一个 user)
  outputs/v2_4dim_summary.md   (4 张排序表 + 算法建议)

Usage:
  python3 scripts/v2_cross_model_4dim.py                       # 默认扫 outputs/
  python3 scripts/v2_cross_model_4dim.py --output-dir /path    # 自定义
  python3 scripts/v2_cross_model_4dim.py --models a,b,c        # 子集
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def safe_dirname(user_id: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]", "_", user_id)
    return s[:128] if len(s) > 128 else s


def percentile(sorted_vals: list, pct: float) -> int:
    if not sorted_vals:
        return 0
    if pct <= 0:
        return sorted_vals[0]
    if pct >= 100:
        return sorted_vals[-1]
    k = (len(sorted_vals) - 1) * pct / 100.0
    lo = int(k)
    return sorted_vals[lo]


def padded_p80(values: list[int], total_buckets: int) -> int:
    """P80 over sparse series padded with zero buckets (matches HTML §4/§5 logic)."""
    if not values:
        return 0
    sorted_v = sorted(values)
    denom = max(total_buckets, len(sorted_v)) or 1
    n_zeros = max(0, denom - len(sorted_v))
    idx = int((denom - 1) * 0.80)
    if idx < n_zeros:
        return 0
    return sorted_v[idx - n_zeros]


def discover_models(output_dir: Path, models_filter: list[str] | None) -> list[Path]:
    """Return list of <model>/per_user_reports/ paths with model_report.json present."""
    candidates = []
    for d in output_dir.iterdir():
        if not d.is_dir():
            continue
        pur = d / "per_user_reports"
        if not (pur / "model_report.json").exists():
            continue
        if models_filter and d.name not in models_filter:
            continue
        candidates.append(pur)
    return sorted(candidates, key=lambda p: p.parent.name)


def collect_rows(model_pur: Path) -> tuple[dict, list[dict]]:
    """For one model, return (model_report, user_rows[])."""
    model_report = json.load(open(model_pur / "model_report.json"))
    user_summary_path = model_pur / "user_summary.json"
    if not user_summary_path.exists():
        return model_report, []
    summary = json.load(open(user_summary_path))

    rows = []
    for u in summary.get("selected_users", []):
        uid = u["user_id"]
        udir = model_pur / safe_dirname(uid)
        rp = udir / "user_report.json"
        if not rp.exists():
            continue
        report = json.load(open(rp))
        s = report.get("stats") or {}
        cfs = report.get("chain_forest_summary") or {}
        rec = report.get("step3_recommendation") or {}
        cls = report.get("classifications") or {}

        # Recompute user-level new_block/s P80 from raw time_series (padded)
        ts_nbs = (report.get("time_series") or {}).get("new_unique_blocks_per_second") or []
        earliest = s.get("earliest_timestamp", 0) or 0
        latest = s.get("latest_timestamp", 0) or 0
        total_sec = (latest - earliest + 1) if latest > earliest else len(ts_nbs)
        new_block_p80 = padded_p80([d["count"] for d in ts_nbs], total_sec)

        # User-level traffic spike summary
        user_spikes = report.get("user_traffic_spikes") or []
        max_spike_ratio = max(
            (sp["ratio_to_prev"] for sp in user_spikes), default=0
        )

        rows.append({
            "model":              model_pur.parent.name,
            "user_id":            uid,
            "request_count":      s.get("total_requests", 0),
            "request_pct":        u.get("request_pct", 0),
            "ideal_hit_rate":     s.get("ideal_hit_rate", 0) or 0,
            "total_blocks":       s.get("total_blocks", 0),
            "unique_blocks":      s.get("unique_blocks", 0),
            "share_of_model_unique": s.get("share_of_model_unique", 0) or 0,
            "new_block_p80":      new_block_p80,
            "rpm_avg":            s.get("rpm_avg", 0),
            "unique_rpm_avg":     s.get("unique_rpm_avg", 0),
            "chain_count":        cfs.get("total_chains", 0),
            "dominant_chain_length": cfs.get("dominant_chain_length", 0),
            "dominant_chain_cov_pct": cfs.get("dominant_chain_coverage_pct", 0),
            "chain_length_ratio": cfs.get("chain_length_ratio", 0),
            "is_anomaly":         cls.get("is_anomaly", False),
            "a_subtype":          rec.get("a_subtype", ""),
            "b_subtype":          rec.get("b_subtype", ""),
            "c_subtype":          rec.get("c_subtype", ""),
            "recommended_queue_count": rec.get("recommended_queue_count", 0),
            "user_spike_count":   len(user_spikes),
            "user_spike_max_ratio": max_spike_ratio,
        })
    return model_report, rows


def write_csv(all_rows: list[dict], out_path: Path) -> None:
    if not all_rows:
        print("(no rows)", flush=True)
        return
    fields = list(all_rows[0].keys())
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(f"  CSV → {out_path} ({len(all_rows)} rows)", flush=True)


def fmt_uid(uid: str, width: int = 36) -> str:
    return uid if len(uid) <= width else uid[:width - 3] + "..."


def write_markdown(
    all_rows: list[dict], model_reports: dict[str, dict], out_path: Path,
) -> None:
    """4 张排序表 + 算法建议."""
    lines: list[str] = []
    lines.append("# v2 工具 — 跨模型 4 维度汇总\n")
    lines.append(f"覆盖模型: **{len(model_reports)}** 个 — "
                 f"`{', '.join(sorted(model_reports.keys()))}`\n")
    lines.append(f"覆盖 user: **{len(all_rows)}** 个 (Top-K 且 ≥ 1% 流量).\n")

    # ============== 维度 1: hit_rate desc ==============
    lines.append("\n## 维度 1: user-level KV cache 命中率排序 (优化空间大→小)\n")
    lines.append("> hit_rate 高 = 业务本身有 prefix cache 复用价值, 优化收益验证应优先在此入手 "
                 "(节约 routing/池化实验成本).\n")
    by_hit = sorted(all_rows, key=lambda r: -r["ideal_hit_rate"])
    lines.append(
        "| # | model | user_id | hit_rate | req% | unique_blocks | chains | 子类型推荐 (A/B/C) | 反常 |\n"
        "|---|---|---|---|---|---|---|---|---|"
    )
    for i, r in enumerate(by_hit[:30], 1):
        a = (r["a_subtype"] or "").split(" (")[0].split(" ")[0]
        b = (r["b_subtype"] or "").split(" ")[0]
        c = (r["c_subtype"] or "").split(" ")[0] if r["c_subtype"] else "—"
        anomaly = "⚠️" if r["is_anomaly"] else ""
        lines.append(
            f"| {i} | {r['model']} | `{fmt_uid(r['user_id'])}` | "
            f"**{r['ideal_hit_rate']:.4f}** | {r['request_pct']:.1f}% | "
            f"{r['unique_blocks']:,} | {r['chain_count']} | "
            f"{a} / {b} / {c} | {anomaly} |"
        )
    if len(by_hit) > 30:
        lines.append(f"\n_(显示 Top 30, 共 {len(by_hit)} user)_")

    lines.append("\n### 建议 routing 实验候选:\n")
    rout_cand = [r for r in by_hit if r["ideal_hit_rate"] >= 0.60 and "A(" in (r["a_subtype"] or "")]
    if rout_cand:
        for r in rout_cand[:10]:
            a = r["a_subtype"].split(" (")[0].split(" ")[0] if r["a_subtype"] else "—"
            lines.append(
                f"- **{r['model']} / `{fmt_uid(r['user_id'])}`** "
                f"hit={r['ideal_hit_rate']:.4f}, share={r['share_of_model_unique']*100:.1f}%, "
                f"推 {a} (chain {r['chain_count']} 条 / dom_len {r['dominant_chain_length']})"
            )
    else:
        lines.append("_无 hit_rate ≥ 0.60 + A 子类型触发的 user_")

    # ============== 维度 2: cache 压力排序 ==============
    lines.append("\n\n## 维度 2: user-level cache 压力排序 (P80 new_block/s)\n")
    lines.append("> cache 压力大 + hit_rate 高 → C(1) 强池化候选 (扩容显存能直接转化为命中率).\n"
                 "> cache 压力大 + hit_rate 低 → 污染源 → A(1) isolation 候选.\n")
    by_pressure = sorted(all_rows, key=lambda r: -r["new_block_p80"])
    lines.append(
        "| # | model | user_id | new_block/s P80 | unique_blocks | hit_rate | share | 池化候选 (C(1)) |\n"
        "|---|---|---|---|---|---|---|---|"
    )
    for i, r in enumerate(by_pressure[:30], 1):
        c1_cand = "✅" if (r["new_block_p80"] >= 100 and r["ideal_hit_rate"] >= 0.60) else ""
        lines.append(
            f"| {i} | {r['model']} | `{fmt_uid(r['user_id'])}` | "
            f"**{r['new_block_p80']:,}** | {r['unique_blocks']:,} | "
            f"{r['ideal_hit_rate']:.4f} | {r['share_of_model_unique']*100:.1f}% | {c1_cand} |"
        )
    if len(by_pressure) > 30:
        lines.append(f"\n_(显示 Top 30, 共 {len(by_pressure)} user)_")

    lines.append("\n### 建议 池化实验候选 (cache 压力大 + hit 高):\n")
    pool_cand = [r for r in by_pressure if r["new_block_p80"] >= 100 and r["ideal_hit_rate"] >= 0.60]
    if pool_cand:
        for r in pool_cand[:10]:
            lines.append(
                f"- **{r['model']} / `{fmt_uid(r['user_id'])}`** "
                f"new_block/s P80={r['new_block_p80']:,} + hit={r['ideal_hit_rate']:.4f} "
                f"+ unique={r['unique_blocks']:,} → 扩容直接提升 hit"
            )
    else:
        lines.append("_无 new_block/s P80 ≥ 100 且 hit ≥ 0.60 的 user (smoke test 数据量太小)_")

    # ============== 维度 3: reuse_inversion ==============
    lines.append("\n\n## 维度 3: 模型级用户复用偏斜 (reuse_inversion_ratio)\n")
    lines.append("> ratio ≥ 2.0 → 用户间复用率差距大, 低 hit + 高流量 user 驱逐其他 user 的 chain.\n"
                 "> → A(1) isolation routing 候选模型.\n")
    inv_rows = []
    for model, mr in model_reports.items():
        ratio = mr.get("reuse_inversion_ratio")
        inv = mr.get("reuse_inversion", False)
        n_total = mr.get("n_users_total", mr.get("n_users", 0))
        n_sel = mr.get("n_users", 0)
        m_hit = mr.get("ideal_hit_rate_aggregate", 0) or 0
        # numeric ratio for sort
        try:
            r_num = float(ratio)
        except (ValueError, TypeError):
            r_num = float("inf") if (inv and isinstance(ratio, str)) else 1.0
        inv_rows.append({
            "model": model, "ratio": ratio, "ratio_num": r_num,
            "inv": inv, "n_total": n_total, "n_sel": n_sel,
            "m_hit": m_hit, "model_report": mr,
        })
    by_inv = sorted(inv_rows, key=lambda r: -r["ratio_num"])
    lines.append(
        "| # | model | n_users (total/sel) | model_hit | ratio | inversion | A(1) 候选 user |\n"
        "|---|---|---|---|---|---|---|"
    )
    for i, mr in enumerate(by_inv, 1):
        a1_users = [
            r for r in all_rows
            if r["model"] == mr["model"]
            and "A(1)" in (r["a_subtype"] or "")
        ]
        a1_str = ", ".join(f"`{fmt_uid(r['user_id'], 18)}`" for r in a1_users[:3])
        if not a1_str:
            a1_str = "—"
        flag = "⚠️ 触发" if mr["inv"] else ""
        lines.append(
            f"| {i} | {mr['model']} | {mr['n_total']}/{mr['n_sel']} | "
            f"{mr['m_hit']:.4f} | **{mr['ratio']}** | {flag} | {a1_str} |"
        )

    # ============== 维度 4: 流量突变 ==============
    lines.append("\n\n## 维度 4: 用户流量突变排序\n")
    lines.append("> spike_count > 0 → 流量瞬时跳变 (≥ 5min × 5×) → 动态扩缩容候选.\n"
                 "> 注意区分: model 级突变 (整体业务流量瞬变) vs user 级突变 (单 user 突发).\n")
    spike_rows = [r for r in all_rows if r["user_spike_count"] > 0]
    by_spike = sorted(spike_rows, key=lambda r: (-r["user_spike_count"], -r["user_spike_max_ratio"]))
    if by_spike:
        lines.append(
            "| # | model | user_id | spike_count | max_ratio | req% | hit_rate |\n"
            "|---|---|---|---|---|---|---|"
        )
        for i, r in enumerate(by_spike[:30], 1):
            lines.append(
                f"| {i} | {r['model']} | `{fmt_uid(r['user_id'])}` | "
                f"**{r['user_spike_count']}** | ×{r['user_spike_max_ratio']} | "
                f"{r['request_pct']:.1f}% | {r['ideal_hit_rate']:.4f} |"
            )
    else:
        lines.append("\n_无 user 级流量突变 (所有 user 流量稳定)._\n")

    # Model-level spikes
    lines.append("\n### 模型级流量突变 (整体业务流量)\n")
    m_spike_rows = []
    for model, mr in model_reports.items():
        m_spikes = mr.get("traffic_spikes") or []
        if m_spikes:
            max_ratio = max(sp["ratio_to_prev"] for sp in m_spikes)
            m_spike_rows.append({
                "model": model, "count": len(m_spikes), "max_ratio": max_ratio,
            })
    if m_spike_rows:
        m_spike_rows.sort(key=lambda r: (-r["count"], -r["max_ratio"]))
        lines.append(
            "| model | spike_count | max_ratio |\n|---|---|---|"
        )
        for r in m_spike_rows:
            lines.append(f"| {r['model']} | {r['count']} | ×{r['max_ratio']} |")
    else:
        lines.append("_所有模型流量稳定, 无 ≥ 5× 模型级突变._\n")

    # ============== 综合算法实验优先级 ==============
    lines.append("\n\n## 综合算法实验优先级\n")
    lines.append("基于以上 4 维度交叉, 建议下列算法实验顺序 (按 ROI 排):\n")

    summary_pts = []
    # A(1) isolation 候选 = 维度 3 inversion + 维度 4 spike (污染源同时是 spike 源)
    a1_inversion = [r for r in all_rows
                    if "A(1)" in (r["a_subtype"] or "")
                    and r["ideal_hit_rate"] < 0.30]
    if a1_inversion:
        summary_pts.append(
            "1. **A(1) isolation routing**: " +
            ", ".join(f"{r['model']}/`{fmt_uid(r['user_id'], 20)}`"
                      for r in a1_inversion[:5])
        )

    # B(2) 多 chain pin 候选 = chain_count ≥ 3 + cov ≥ 10%
    b2_cand = [r for r in all_rows
               if "B(2)" in (r["b_subtype"] or "")
               and r["recommended_queue_count"] >= 2]
    if b2_cand:
        summary_pts.append(
            "2. **B(2) 多队列 LRU**: " +
            ", ".join(
                f"{r['model']}/`{fmt_uid(r['user_id'], 20)}` (Q={r['recommended_queue_count']})"
                for r in b2_cand[:5]
            )
        )

    # C(1) 强池化候选
    if pool_cand:
        summary_pts.append(
            "3. **C(1) 强池化**: " +
            ", ".join(
                f"{r['model']}/`{fmt_uid(r['user_id'], 20)}`"
                for r in pool_cand[:5]
            )
        )

    # 动态扩缩容
    if by_spike:
        summary_pts.append(
            "4. **动态扩缩容** (spike): " +
            ", ".join(
                f"{r['model']}/`{fmt_uid(r['user_id'], 20)}` (×{r['user_spike_max_ratio']})"
                for r in by_spike[:5]
            )
        )

    if summary_pts:
        for pt in summary_pts:
            lines.append(pt + "\n")
    else:
        lines.append("_未检出明显优先候选, 建议人工逐项 inspect HTML._\n")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Markdown → {out_path}", flush=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--output-dir", type=Path, default=ROOT / "outputs",
                   help="Base outputs/ directory (default: ./outputs)")
    p.add_argument("--models", type=str, default="",
                   help="Comma-separated model dir names (default: all under output-dir)")
    p.add_argument("--summary-csv", type=Path, default=None,
                   help="CSV output path (default: outputs/v2_4dim_summary.csv)")
    p.add_argument("--summary-md", type=Path, default=None,
                   help="Markdown output path (default: outputs/v2_4dim_summary.md)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    models_filter = [m.strip() for m in args.models.split(",") if m.strip()] or None

    models_purs = discover_models(args.output_dir, models_filter)
    if not models_purs:
        print(f"No models found under {args.output_dir} "
              f"(filter: {models_filter or 'none'})", file=sys.stderr)
        sys.exit(1)

    print(f"Discovered {len(models_purs)} model(s):", flush=True)
    for p in models_purs:
        print(f"  - {p.parent.name}", flush=True)

    all_rows = []
    model_reports = {}
    for pur in models_purs:
        mr, rows = collect_rows(pur)
        model_reports[pur.parent.name] = mr
        all_rows.extend(rows)
        print(f"  {pur.parent.name}: {len(rows)} user(s)", flush=True)

    csv_path = args.summary_csv or (args.output_dir / "v2_4dim_summary.csv")
    md_path  = args.summary_md  or (args.output_dir / "v2_4dim_summary.md")
    write_csv(all_rows, csv_path)
    write_markdown(all_rows, model_reports, md_path)

    print(f"\nTotal: {len(all_rows)} user × {len(model_reports)} model = "
          f"see {md_path} for 4-dimension sorted tables", flush=True)


if __name__ == "__main__":
    main()
