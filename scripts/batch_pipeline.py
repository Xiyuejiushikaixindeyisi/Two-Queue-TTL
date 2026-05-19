#!/usr/bin/env python3
"""Batch pipeline: convert → registry → analyze → cross-model comparison.

Directory convention
--------------------
  data/<model_dir>/raw/*.csv          ← raw production CSVs (Chinese column names OK)
  data/<model_dir>/trace1_production.csv       ← converted standard trace
  data/<model_dir>/trace1_production_registry.txt  ← offline block registry
  outputs/<model_dir>/trace_analysis/trace_summary.json  ← analysis results

Pipeline steps (per model)
--------------------------
  Step 1  Convert raw/*.csv → trace1_production.csv
  Step 2  Generate registry → trace1_production_registry.txt
  Step 3  Run multitenant trace analysis
  Step 4  (global) Cross-model comparison table

Usage
-----
  # Full pipeline for all models
  python scripts/batch_pipeline.py

  # Specific models only
  python scripts/batch_pipeline.py --models qwen_v3_32b_32k qwen_v3.5_27b_64k

  # Skip conversion if traces already exist
  python scripts/batch_pipeline.py --skip-convert

  # Skip analysis if summaries already exist
  python scripts/batch_pipeline.py --skip-analysis

  # Only print comparison (all steps already done)
  python scripts/batch_pipeline.py --compare-only
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data"
OUTPUTS = ROOT / "outputs"
SCRIPTS = ROOT / "scripts"

# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

MODELS: list[dict] = [
    {
        "name":       "Qwen-V3.5-27B-64K",
        "dir":        "qwen_v3.5_27b_64k",
        "ctx_k":      64,
        "block_size": 128,
    },
    {
        "name":       "Qwen-V3.5-27B-128K",
        "dir":        "qwen_v3.5_27b_128k",
        "ctx_k":      128,
        "block_size": 128,
    },
    {
        "name":       "Qwen-V3-32B-8K",
        "dir":        "qwen_v3_32b_8k",
        "ctx_k":      8,
        "block_size": 128,
    },
    {
        "name":       "Qwen-V3-32B-32K",
        "dir":        "qwen_v3_32b_32k",
        "ctx_k":      32,
        "block_size": 128,
    },
    {
        "name":       "DeepSeek-V3.1-8K",
        "dir":        "deepseek_v3.1_8k",
        "ctx_k":      8,
        "block_size": 128,
    },
    {
        "name":       "DeepSeek-V3.1-32K",
        "dir":        "deepseek_v3.1_32k",
        "ctx_k":      32,
        "block_size": 128,
    },
]


def model_by_dir(dirs: list[str]) -> list[dict]:
    return [m for m in MODELS if m["dir"] in dirs]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], desc: str, env: dict | None = None) -> bool:
    """Run a subprocess. Return True on success."""
    print(f"  → {desc}", flush=True)
    t0 = time.time()
    e = {**os.environ, **(env or {})}
    result = subprocess.run(cmd, env=e)
    elapsed = time.time() - t0
    if result.returncode != 0:
        print(f"    ✗ FAILED  ({elapsed:.1f}s)", flush=True)
        return False
    print(f"    ✓ done  ({elapsed:.1f}s)", flush=True)
    return True


def section(title: str) -> None:
    print(f"\n{'═'*70}\n  {title}\n{'═'*70}", flush=True)


# ---------------------------------------------------------------------------
# Step 1: Convert raw CSVs
# ---------------------------------------------------------------------------

def step_convert(model: dict) -> bool:
    """Merge all raw/*.csv into trace1_production.csv via convert_raw_trace.py."""
    raw_dir = DATA / model["dir"] / "raw"
    out_csv = DATA / model["dir"] / "trace1_production.csv"

    raw_files = sorted(raw_dir.glob("*.csv"))
    if not raw_files:
        print(f"  [SKIP] No raw CSVs in {raw_dir}", flush=True)
        return False

    print(f"  Found {len(raw_files)} raw file(s): "
          f"{[f.name for f in raw_files]}", flush=True)

    # If single file, convert directly.
    # If multiple files, convert each to a tmp file then concatenate.
    if len(raw_files) == 1:
        ok = run(
            [sys.executable, str(SCRIPTS / "convert_raw_trace.py"),
             "--input",  str(raw_files[0]),
             "--output", str(out_csv),
             "--block-size", str(model["block_size"])],
            f"convert {raw_files[0].name}",
            env={"DISABLE_TIKTOKEN": "1"},
        )
        return ok

    # Multiple files: convert each to tmp, then merge header-aware.
    tmp_files = []
    for f in raw_files:
        tmp = out_csv.parent / f"_tmp_{f.stem}.csv"
        ok = run(
            [sys.executable, str(SCRIPTS / "convert_raw_trace.py"),
             "--input",  str(f),
             "--output", str(tmp),
             "--block-size", str(model["block_size"])],
            f"convert {f.name}",
            env={"DISABLE_TIKTOKEN": "1"},
        )
        if not ok:
            return False
        tmp_files.append(tmp)

    # Concatenate: first file with header, rest without.
    print(f"  → merging {len(tmp_files)} converted files …", flush=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as out:
        for i, tmp in enumerate(tmp_files):
            with open(tmp, newline="", encoding="utf-8") as src:
                reader = csv.reader(src)
                header = next(reader)
                if i == 0:
                    csv.writer(out).writerow(header)
                for row in reader:
                    csv.writer(out).writerow(row)
            tmp.unlink()

    print(f"    ✓ merged → {out_csv.name}", flush=True)
    return True


# ---------------------------------------------------------------------------
# Step 2: Generate registry
# ---------------------------------------------------------------------------

def step_registry(model: dict) -> bool:
    trace_csv = DATA / model["dir"] / "trace1_production.csv"
    registry  = DATA / model["dir"] / "trace1_production_registry.txt"

    if not trace_csv.exists():
        print(f"  [SKIP] trace not found: {trace_csv}", flush=True)
        return False

    return run(
        [sys.executable, str(SCRIPTS / "generate_registry.py"),
         "--trace",  str(trace_csv),
         "--output", str(registry)],
        f"generate registry → {registry.name}",
    )


# ---------------------------------------------------------------------------
# Step 3: Trace analysis
# ---------------------------------------------------------------------------

def step_analysis(model: dict) -> bool:
    trace_csv  = DATA / model["dir"] / "trace1_production.csv"
    output_dir = OUTPUTS / model["dir"] / "trace_analysis"

    if not trace_csv.exists():
        print(f"  [SKIP] trace not found: {trace_csv}", flush=True)
        return False

    return run(
        [sys.executable, str(SCRIPTS / "analyze_multitenant_trace.py"),
         "--trace",      str(trace_csv),
         "--heavy-pct",  "10",
         "--block-size", str(model["block_size"]),
         "--output-dir", str(output_dir)],
        f"trace analysis → {output_dir.relative_to(ROOT)}",
    )


# ---------------------------------------------------------------------------
# Step 4: Cross-model comparison
# ---------------------------------------------------------------------------

def load_summary(model: dict) -> dict | None:
    path = OUTPUTS / model["dir"] / "trace_analysis" / "trace_summary.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def print_comparison(models: list[dict]) -> None:
    summaries = {m["dir"]: load_summary(m) for m in models}
    available = {d: s for d, s in summaries.items() if s is not None}

    if not available:
        print("\n  No trace_summary.json files found yet.", flush=True)
        return

    section("CROSS-MODEL COMPARISON")

    # ── Table 1: Overview ────────────────────────────────────────────────
    cols = ["Model", "Users", "Requests", "Heavy%→Req%",
            "∞ Hit Rate", "Reuse×", "Heavy×", "Light×"]
    widths = [26, 7, 10, 14, 11, 8, 8, 8]
    _hdr(cols, widths)
    for m in models:
        s = available.get(m["dir"])
        if s is None:
            _row([m["name"], "—"*7], widths)
            continue
        u   = s.get("user", {})
        blk = s.get("blocks", {})
        _row([
            m["name"],
            f"{u.get('total_users', '?'):,}",
            f"{u.get('total_requests', '?'):,}",
            f"{u.get('heavy_pct', 10):.0f}%→{u.get('heavy_req_pct', 0):.1f}%",
            f"{s.get('infinite_hit_rate', 0):.4f}",
            f"{s.get('reuse_multiplier', 0):.2f}x",
            f"{blk.get('heavy_reuse_multiplier', 0):.2f}x",
            f"{blk.get('light_reuse_multiplier', 0):.2f}x",
        ], widths)

    # ── Table 2: Reuse time & TTL recommendations ────────────────────────
    print()
    cols2 = ["Model", "p50 reuse", "p80 reuse", "p95 reuse",
             "base_ttl", "ext_ttl", "Heavy gap p50", "Heavy gap p95"]
    widths2 = [26, 10, 10, 10, 10, 10, 14, 14]
    _hdr(cols2, widths2)
    for m in models:
        s = available.get(m["dir"])
        if s is None:
            _row([m["name"], "—"*7], widths2)
            continue
        rt  = s.get("reuse_times", {})
        ses = s.get("session", {})
        _row([
            m["name"],
            f"{rt.get('p50', 0):.0f}s",
            f"{rt.get('p80', 0):.0f}s",
            f"{rt.get('p95', 0):.0f}s",
            f"{rt.get('recommended_base_ttl', 0):.0f}s",
            f"{rt.get('recommended_extended_ttl', 0):.0f}s",
            _fmt_s(ses.get("heavy_gap_p50")),
            _fmt_s(ses.get("heavy_gap_p95")),
        ], widths2)

    # ── Table 3: Working set & capacity ──────────────────────────────────
    print()
    cols3 = ["Model", "ctx_K", "Heavy WS (blocks)", "Full WS (blocks)",
             "Insertion rate", "∞ hit rate"]
    widths3 = [26, 7, 19, 19, 16, 12]
    _hdr(cols3, widths3)
    for m in models:
        s = available.get(m["dir"])
        if s is None:
            _row([m["name"], "—"*5], widths3)
            continue
        blk = s.get("blocks", {})
        _row([
            m["name"],
            f"{m['ctx_k']}K",
            f"{blk.get('heavy_unique_blocks', 0):,}",
            f"{blk.get('total_unique', 0):,}",
            f"{len(s.get('future_index', [])) / max(s.get('time_span_s', 1), 1):.1f} blk/s"
            if False else _est_rate(s),
            f"{s.get('infinite_hit_rate', 0):.4f}",
        ], widths3)

    print(f"\n  Available: {len(available)}/{len(models)} models", flush=True)
    missing = [m["name"] for m in models if m["dir"] not in available]
    if missing:
        print(f"  Missing:   {missing}", flush=True)


def _est_rate(s: dict) -> str:
    blk = s.get("blocks", {})
    t   = s.get("time_span_s", 0)
    uniq = blk.get("total_unique", 0)
    if t <= 0 or uniq <= 0:
        return "—"
    return f"{uniq/t:.1f} blk/s"


def _fmt_s(v) -> str:
    if v is None:
        return "—"
    return f"{v:.0f}s"


def _hdr(cols: list[str], widths: list[int]) -> None:
    sep = "  " + "  ".join("─" * w for w in widths)
    print("  " + "  ".join(f"{c:<{w}}" for c, w in zip(cols, widths)))
    print(sep)


def _row(cells: list[str], widths: list[int]) -> None:
    # Pad or truncate cells to match widths
    padded = []
    for i, w in enumerate(widths):
        c = cells[i] if i < len(cells) else ""
        padded.append(f"{c:<{w}}"[:w])
    print("  " + "  ".join(padded))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Batch pipeline: convert → registry → analyze → compare",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--models", nargs="+", default=None,
        metavar="DIR",
        help="Model dir names to process (default: all). "
             f"Choices: {[m['dir'] for m in MODELS]}",
    )
    p.add_argument("--skip-convert",  action="store_true",
                   help="Skip Step 1 (raw CSV conversion)")
    p.add_argument("--skip-registry", action="store_true",
                   help="Skip Step 2 (registry generation)")
    p.add_argument("--skip-analysis", action="store_true",
                   help="Skip Step 3 (trace analysis)")
    p.add_argument("--compare-only",  action="store_true",
                   help="Only print comparison table (skip all steps)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    targets = model_by_dir(args.models) if args.models else MODELS
    if not targets:
        print(f"No matching models for: {args.models}")
        print(f"Available: {[m['dir'] for m in MODELS]}")
        sys.exit(1)

    if args.compare_only:
        print_comparison(targets)
        return

    for m in targets:
        section(f"{m['name']}  ({m['dir']})")

        # Step 1: Convert
        if not args.skip_convert:
            print("\n  [Step 1] Convert raw CSVs", flush=True)
            step_convert(m)
        else:
            trace_csv = DATA / m["dir"] / "trace1_production.csv"
            status = "exists" if trace_csv.exists() else "MISSING"
            print(f"\n  [Step 1] Skipped — trace1_production.csv: {status}",
                  flush=True)

        # Step 2: Registry
        if not args.skip_registry:
            print("\n  [Step 2] Generate registry", flush=True)
            step_registry(m)
        else:
            print("\n  [Step 2] Skipped", flush=True)

        # Step 3: Analysis
        if not args.skip_analysis:
            print("\n  [Step 3] Trace analysis", flush=True)
            step_analysis(m)
        else:
            print("\n  [Step 3] Skipped", flush=True)

    # Step 4: Comparison
    print_comparison(targets)


if __name__ == "__main__":
    main()
