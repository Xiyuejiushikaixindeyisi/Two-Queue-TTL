#!/usr/bin/env python3
"""Step 1.5 v2 — 7-model batch pipeline + cross-model summary.

Runs per_user_report_analyzer.py + render_user_report_html.py for each
production model, then collects all selected-user recommendations into a
single CSV + Markdown table for cross-model comparison.

Use this to verify v2 工具 produces results consistent with
docs/step3_algorithm_decision_matrix.md §9.2.5 reference table.

Usage
-----
    # Full pipeline (default models, default thresholds)
    python3 scripts/v2_run_pipeline.py

    # Skip analyzer (just renderer + summary, if analyzer output exists)
    python3 scripts/v2_run_pipeline.py --skip-analyzer

    # Skip both (summary-only, post-process existing outputs)
    python3 scripts/v2_run_pipeline.py --skip-analyzer --skip-renderer

    # Custom subset
    python3 scripts/v2_run_pipeline.py --models qwen_v3_32b_8k,deepseek_v3.1_8k

    # Override thresholds (forwarded to analyzer)
    python3 scripts/v2_run_pipeline.py --analyzer-extra='--spike-threshold 3.0'

Outputs
-------
    outputs/v2_summary.csv     — every selected user across all models
    outputs/v2_summary.md      — markdown tables grouped by model
                                  + cross-model subtype distribution

After running, compare against §9.2.5 reference table in
docs/step3_algorithm_decision_matrix.md and fill §9.2.8 偏差日志
with any user whose subtype differs from expectation.
"""
from __future__ import annotations

import argparse
import csv
import json
import shlex
import subprocess
import sys
from pathlib import Path

# Default model list — adjust to match your production data/ subdir names.
# (DSK / GLM may use date-tagged names like dsk8k_24h_0506.)
DEFAULT_MODELS = [
    "qwen_v3_8b_8k",
    "qwen_v3_32b_8k",
    "qwen_v3_32b_32k",
    "qwen_v3.5_27b_64k",
    "qwen_v3.5_27b_128k",
    "deepseek_v3.1_8k",
    "deepseek_v3.1_32k",
    "glm_v5.1",
]

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def safe_dirname_local(user_id: str) -> str:
    """Mirror of per_user_report_analyzer.safe_dirname (avoid import side-effects)."""
    import re
    s = re.sub(r"[^A-Za-z0-9._-]", "_", user_id)
    return s[:128] if len(s) > 128 else s


def run_analyzer(
    data_dir: Path, output_dir: Path, model: str, extra: list[str],
    encoder: str = "byte", tokenizer_path: str = "models/glm5_tokenizer",
    chat_mode: str = "wrap_user",
) -> bool:
    raw = data_dir / model / "raw"
    if not raw.exists():
        print(f"  ⚠️  {model}: raw dir not found at {raw}", flush=True)
        return False
    cmd = [
        sys.executable, "scripts/per_user_report_analyzer.py",
        "--raw-csv", str(raw),
        "--output-dir", str(output_dir / model / "per_user_reports"),
        "--encoder", encoder,
    ]
    if encoder == "glm5_token":
        cmd += ["--tokenizer-path", tokenizer_path, "--chat-mode", chat_mode]
    cmd += extra
    print(f"  $ {' '.join(shlex.quote(c) for c in cmd)}", flush=True)
    res = subprocess.run(cmd, cwd=str(ROOT))
    return res.returncode == 0


def run_renderer(output_dir: Path, model: str) -> bool:
    base = output_dir / model / "per_user_reports"
    if not (base / "user_summary.json").exists():
        print(f"  ⚠️  {model}: analyzer output missing, skipping render", flush=True)
        return False
    cmd = [
        sys.executable, "scripts/render_user_report_html.py",
        "--input-dir", str(base),
    ]
    print(f"  $ {' '.join(shlex.quote(c) for c in cmd)}", flush=True)
    res = subprocess.run(cmd, cwd=str(ROOT))
    return res.returncode == 0


def collect_summary_rows(output_dir: Path, model: str) -> list[dict]:
    """For one model, read every user_report.json + chain_forest_summary,
    return one row per selected user with all v2 fields."""
    base = output_dir / model / "per_user_reports"
    summary_path = base / "user_summary.json"
    if not summary_path.exists():
        return []
    summary = json.load(open(summary_path, encoding="utf-8"))
    rows: list[dict] = []
    for user_row in summary.get("selected_users", []):
        uid = user_row["user_id"]
        user_dir = base / safe_dirname_local(uid)
        report_path = user_dir / "user_report.json"
        if not report_path.exists():
            continue
        report = json.load(open(report_path, encoding="utf-8"))
        s = report.get("stats") or {}
        cfs = report.get("chain_forest_summary") or {}
        cls = report.get("classifications") or {}
        rec = report.get("step3_recommendation") or {}
        rows.append({
            "model":                 model,
            "user_id":               uid,
            "req_pct":               user_row.get("request_pct", 0),
            "hit":                   s.get("ideal_hit_rate"),
            "share_of_model_unique": s.get("share_of_model_unique"),
            "rpm_avg":               s.get("rpm_avg"),
            "unique_rpm_avg":        s.get("unique_rpm_avg"),
            "chain_count":           cfs.get("total_chains"),
            "dom_len":               cfs.get("dominant_chain_length"),
            "cov_pct":               cfs.get("dominant_chain_coverage_pct"),
            "chain_length_ratio":    cfs.get("chain_length_ratio"),
            "hit_band":              cls.get("hit_band"),
            "cov_band":              cls.get("cov_band"),
            "chain_len_band":        cls.get("chain_len_band"),
            "unique_share_band":     cls.get("unique_share_band"),
            "chain_count_band":      cls.get("chain_count_band"),
            "a_subtype":             rec.get("a_subtype"),
            "b_subtype":             rec.get("b_subtype"),
            "c_subtype":             rec.get("c_subtype"),
            "primary_algorithm":     rec.get("primary_algorithm"),
            "companion_algorithm":   rec.get("companion_algorithm"),
            "is_anomaly":            rec.get("is_anomaly"),
        })
    return rows


def write_csv(rows: list[dict], out_path: Path) -> None:
    if not rows:
        print("  (no rows to write)", flush=True)
        return
    fieldnames = list(rows[0].keys())
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    print(f"  CSV → {out_path} ({len(rows)} rows)", flush=True)


def write_markdown(
    rows: list[dict], out_path: Path, models_ordered: list[str],
    output_dir: Path,
) -> None:
    """One section per model + a cross-model subtype distribution summary."""
    by_model: dict[str, list[dict]] = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("# v2 工具实测汇总\n\n")
        f.write("> 对照 `docs/step3_algorithm_decision_matrix.md` §9.2.5 检查归类一致性。\n\n")

        # === Per-model tables ===
        for model in models_ordered:
            model_rows = by_model.get(model)
            if not model_rows:
                f.write(f"## {model}\n\n_(no data — analyzer未跑或目录缺失)_\n\n")
                continue
            f.write(f"## {model}\n\n")
            f.write(
                "| user | req% | hit | share% | chain (n/dom/cov%/ratio) | bands | A | B | C | 反常 |\n"
                "|---|---|---|---|---|---|---|---|---|---|\n"
            )
            for r in model_rows:
                share = (r["share_of_model_unique"] or 0) * 100
                bands = "/".join([
                    r.get("hit_band") or "—",
                    r.get("cov_band") or "—",
                    r.get("chain_len_band") or "—",
                    r.get("unique_share_band") or "—",
                    r.get("chain_count_band") or "—",
                ])
                anomaly = "✅" if r.get("is_anomaly") else ""
                a = (r.get("a_subtype") or "").split(" (")[0].split(" ")[0]
                b = (r.get("b_subtype") or "").split(" ")[0]
                c = (r.get("c_subtype") or "").split(" ")[0] if r.get("c_subtype") else "—"
                uid_short = r["user_id"]
                if len(uid_short) > 40:
                    uid_short = uid_short[:37] + "..."
                f.write(
                    f"| `{uid_short}` | {r['req_pct']:.1f}% | "
                    f"{(r['hit'] or 0):.3f} | {share:.1f}% | "
                    f"{r['chain_count']}/{r['dom_len']}/{(r['cov_pct'] or 0):.1f}%/"
                    f"{(r['chain_length_ratio'] or 0):.2f} | "
                    f"{bands} | {a} | {b} | {c} | {anomaly} |\n"
                )
            f.write("\n")

        # === Cross-model subtype distribution ===
        f.write("## 跨模型子类型分布\n\n")
        subtype_counts: dict[str, int] = {}
        for r in rows:
            for sub in (r.get("a_subtype"), r.get("b_subtype"), r.get("c_subtype")):
                if not sub:
                    continue
                # Extract leading token like "A(1)" / "B(2)" / "C(1)" / "A0"
                key = sub.split(" ")[0]
                subtype_counts[key] = subtype_counts.get(key, 0) + 1
        for k in sorted(subtype_counts):
            f.write(f"- `{k}`: {subtype_counts[k]} user(s)\n")
        f.write("\n")

        # === Anomaly users ===
        anomaly_rows = [r for r in rows if r.get("is_anomaly")]
        if anomaly_rows:
            f.write(f"## 反常 user (长 chain + 低 cov + 低 hit) — {len(anomaly_rows)} 个\n\n")
            f.write("| model | user | hit | cov% | chain_ratio |\n|---|---|---|---|---|\n")
            for r in anomaly_rows:
                f.write(
                    f"| {r['model']} | `{r['user_id']}` | "
                    f"{(r['hit'] or 0):.3f} | {(r['cov_pct'] or 0):.1f}% | "
                    f"{(r['chain_length_ratio'] or 0):.2f} |\n"
                )
            f.write("\n建议人工检查 §5 chain decoded 内容判断是否 wrapper boilerplate.\n\n")

        # === Manual-field missing per model ===
        f.write("## 人工补字段缺失情况\n\n")
        f.write(
            "下列字段必须人工补到对应模型的 `outputs/<model>/per_user_reports/model_report.json`, "
            "再重新跑 renderer:\n\n"
        )
        f.write("| model | model_params_class | instance_count | cache_capacity_blocks |\n|---|---|---|---|\n")
        for model in models_ordered:
            mrp = output_dir / model / "per_user_reports" / "model_report.json"
            if not mrp.exists():
                continue
            try:
                m = json.load(open(mrp, encoding="utf-8"))
            except Exception:
                continue
            def fmt(k):
                v = m.get(k)
                return "**缺失**" if v is None else str(v)
            f.write(
                f"| {model} | {fmt('model_params_class')} | "
                f"{fmt('instance_count')} | {fmt('cache_capacity_blocks')} |\n"
            )
        f.write("\n")

    print(f"  Markdown → {out_path}", flush=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--data-dir", type=Path, default=ROOT / "data",
        help="Base directory containing <model>/raw/ subdirs (default: ./data)",
    )
    p.add_argument(
        "--output-dir", type=Path, default=ROOT / "outputs",
        help="Base directory for per-model outputs (default: ./outputs)",
    )
    p.add_argument(
        "--models", type=str, default=",".join(DEFAULT_MODELS),
        help="Comma-separated model dir names. "
             f"Default: {','.join(DEFAULT_MODELS)}",
    )
    p.add_argument(
        "--analyzer-extra", type=str, default="",
        help="Extra args forwarded to per_user_report_analyzer.py "
             "(quote them, e.g., --analyzer-extra='--spike-threshold 3.0')",
    )
    p.add_argument("--skip-analyzer", action="store_true",
                   help="Skip analyzer pass (use existing outputs)")
    p.add_argument("--skip-renderer", action="store_true",
                   help="Skip renderer pass")
    p.add_argument("--skip-summary",  action="store_true",
                   help="Skip cross-model summary generation")
    # Step 1.6: token-level encoding (forwarded to analyzer subprocess)
    p.add_argument("--encoder", type=str, default="byte",
                   choices=["byte", "glm5_token"],
                   help="prompt encoding strategy (default: byte regression baseline)")
    p.add_argument("--tokenizer-path", type=str, default="models/glm5_tokenizer",
                   help="GLM-5 tokenizer path (only used when --encoder=glm5_token)")
    p.add_argument("--chat-mode", type=str, default="wrap_user",
                   choices=["raw", "wrap_user", "messages"],
                   help="chat template wrapping (only used when --encoder=glm5_token)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    extra = shlex.split(args.analyzer_extra) if args.analyzer_extra else []

    print(f"Models: {models}", flush=True)
    print(f"Data dir:   {args.data_dir}", flush=True)
    print(f"Output dir: {args.output_dir}", flush=True)
    print(f"Analyzer extra args: {extra}\n", flush=True)

    # ===== Pass 1: analyzer per model =====
    if not args.skip_analyzer:
        print("=" * 60, flush=True)
        print("Pass 1: per_user_report_analyzer.py", flush=True)
        print("=" * 60, flush=True)
        for model in models:
            print(f"\n[{model}]", flush=True)
            run_analyzer(
                args.data_dir, args.output_dir, model, extra,
                encoder=args.encoder, tokenizer_path=args.tokenizer_path,
                chat_mode=args.chat_mode,
            )

    # ===== Pass 2: renderer per model =====
    if not args.skip_renderer:
        print("\n" + "=" * 60, flush=True)
        print("Pass 2: render_user_report_html.py", flush=True)
        print("=" * 60, flush=True)
        for model in models:
            print(f"\n[{model}]", flush=True)
            run_renderer(args.output_dir, model)

    # ===== Pass 3: cross-model summary =====
    if args.skip_summary:
        return
    print("\n" + "=" * 60, flush=True)
    print("Pass 3: cross-model summary", flush=True)
    print("=" * 60, flush=True)

    all_rows: list[dict] = []
    for model in models:
        rows = collect_summary_rows(args.output_dir, model)
        print(f"  {model}: {len(rows)} selected user(s)", flush=True)
        all_rows.extend(rows)

    csv_path = args.output_dir / "v2_summary.csv"
    md_path  = args.output_dir / "v2_summary.md"
    write_csv(all_rows, csv_path)
    write_markdown(all_rows, md_path, models_ordered=models, output_dir=args.output_dir)

    print(f"\nTotal: {len(all_rows)} user-rows across {len(models)} models", flush=True)
    print(
        f"\nNext: compare {md_path} against decision_matrix.md §9.2.5,"
        f" fill §9.2.8 偏差日志.",
        flush=True,
    )


if __name__ == "__main__":
    main()
