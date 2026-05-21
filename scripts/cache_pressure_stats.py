#!/usr/bin/env python3
"""Cache 压力统计: 每个数据集"每分钟涌入多少 token"的 avg / P50 / P80 / P90.

用途
----
为"要不要做池化 (pooling)"提供依据: 看每个 (模型上下文窗口, 天) 的数据集,
单位时间 (分钟) 涌入 KV cache 的 token 量及其分布峰值 (P90 = 高压时刻).

口径
----
- token 数: 每条请求的 prompt 经 chat template + tokenizer 编码后的 token 数
  (apply_template, 与 hit_rate 分析同一 encoder/chat_mode, 数值一致).
- 分钟桶: floor(timestamp_秒 / 60). 每分钟的 token 涌入 = 落在该分钟内所有
  请求的 token 数之和.
- 分布: 对"每分钟 token 数"序列取 avg / P50 / P80 / P90.
  * --minute-mode span (默认): 桶覆盖 [首请求分钟, 末请求分钟] 全区间, 中间
    没有请求的分钟按 0 计入 → 反映真实时间轴上的速率.
  * --minute-mode active: 只统计有请求的分钟 → 反映"有流量时"的强度.
  两个口径都会输出, 便于对照 (avg/P50... 取决于 --minute-mode 选哪个写进图).

CSV 格式: 4 列 (request_id / user_id / raw_prompt / timestamp), 支持中文别名
+ UTF-8 BOM. timestamp 自动识别 秒/毫秒/微秒.

数据集发现
----------
- --datasets a,b,c: 显式给定. 每个名字解析为 <dir>/<name>/raw/*.csv (或直接是
  一个 .csv 文件路径, 或 <dir>/<name>/*.csv).
- 不给 --datasets: 自动发现 <dir>/*/raw/*.csv, 数据集名 = raw 上一级目录名.

输出
----
JSON (--json-out) 给绘图脚本用; 可选 CSV (--csv-out); terminal 表.

Usage (tokenizer venv, 有 transformers)
---------------------------------------
    PYTHONPATH=. .venv_glm5/bin/python3 scripts/cache_pressure_stats.py \
      --dir data \
      --datasets GLM-5-32K-0513,GLM-5-32K-0514,GLM-5-64K-0513,GLM-5-64K-0514,GLM-5-128K-0513,GLM-5-128K-0514 \
      --encoder glm5_token --tokenizer-path models/glm5_tokenizer --chat-mode wrap_user \
      --json-out outputs/cache_pressure/stats.json

然后绘图 (matplotlib venv):
    .venv/bin/python3 scripts/plot_cache_pressure.py \
      --stats outputs/cache_pressure/stats.json --output-dir outputs/cache_pressure --lang zh
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
csv.field_size_limit(sys.maxsize)

# 注意: transformers 相关 import 延迟到 raw 模式内部, 这样 converted 模式
# (复用 input_length, 不分词) 在任何 venv 都能跑, 不需要 transformers.

# 列名别名 (中文/BOM/常见英文). timestamp 列在生产 CSV 里名字不固定, 多给几个.
ALIASES = {
    "request_id": ["request_id", "请求ID", "请求id", "reqid", "id"],
    "user_id":    ["user_id", "租户ID", "租户id", "用户ID", "用户id", "uid", "tenant_id"],
    "raw_prompt": ["raw_prompt", "请求参数", "prompt", "请求内容", "input", "messages"],
    "timestamp":  ["timestamp", "ts", "time", "时间", "时间戳", "请求时间",
                   "创建时间", "request_time", "time_stamp"],
    # convert_trace.py 产出的 token 数列 (raw/chat 模式都写 input_length)
    "input_length": ["input_length", "token_count", "n_tokens", "tokens", "input_len"],
}


def get_col(row: dict, target: str) -> str | None:
    for name in ALIASES[target]:
        if name in row:
            return row[name]
    return row.get(target)


def percentile(sorted_vals: list[float], q: float) -> float:
    """线性插值百分位 (与 numpy.percentile 默认 'linear' 一致). q ∈ [0,100]."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    rank = (q / 100.0) * (len(sorted_vals) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(sorted_vals[lo])
    frac = rank - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def to_seconds(ts_raw: str, unit: str) -> float | None:
    try:
        v = float(ts_raw)
    except (TypeError, ValueError):
        return None
    if unit == "s":
        return v
    if unit == "ms":
        return v / 1e3
    if unit == "us":
        return v / 1e6
    # auto: 用量级猜 (epoch 秒≈1.7e9, 毫秒≈1.7e12, 微秒≈1.7e15)
    if v >= 1e14:
        return v / 1e6
    if v >= 1e11:
        return v / 1e3
    return v


def parse_size_day(name: str) -> tuple[str | None, str | None, str | None]:
    """从数据集名解析 (size, day, day_label). 例 'GLM-5-32K-0513' → ('32K','0513','5.13').

    按 '-'/'_' 切 token 再判定, 避免把 '128K' 里的 '128' 误当成日期.
    """
    tokens = re.split(r"[-_]", name)
    size = None
    for t in tokens:
        m = re.fullmatch(r"(\d+)[kK]", t)  # 纯 '<数字>K' token 才算 size
        if m:
            size = f"{m.group(1)}K"
            break
    day = day_label = None
    digit_tokens = [t for t in tokens if re.fullmatch(r"\d{3,4}", t)]  # 纯 3-4 位数字 = 日期
    if digit_tokens:
        day = digit_tokens[-1]
        s = day.zfill(4)
        day_label = f"{int(s[:2])}.{int(s[2:])}"  # MMDD → M.D
    return size, day, day_label


def resolve_csvs(token: str, base_dir: Path) -> list[Path]:
    p = Path(token)
    if p.is_file():
        return [p]
    cand = base_dir / token / "raw"
    if cand.is_dir():
        files = sorted(cand.glob("*.csv"))
        if files:
            return files
    cand2 = base_dir / token
    if cand2.is_dir():
        files = sorted(cand2.glob("*.csv"))
        if files:
            return files
    return []


def discover_datasets(base_dir: Path) -> list[str]:
    names = []
    for sub in sorted(base_dir.iterdir()):
        if not sub.is_dir() or sub.name.startswith((".", "out_")) \
                or sub.name.endswith("_tokenizer"):
            continue
        if (sub / "raw").is_dir() and any((sub / "raw").glob("*.csv")):
            names.append(sub.name)
        elif any(sub.glob("*.csv")):
            names.append(sub.name)
    return names


def analyze_dataset(csv_files: list[Path], count_fn, ts_unit: str) -> dict:
    """读 dataset 的所有 CSV → 每分钟 token 涌入序列 → avg/P50/P80/P90 (两种口径).

    count_fn(row) -> int 给出该请求的 token 数 (raw 模式现场分词; converted 模式
    直接读 input_length 列).
    """
    minute_tokens: dict[int, int] = {}
    n_requests = 0
    n_no_ts = 0
    total_tokens = 0
    for csv_path in csv_files:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                ntok = count_fn(row)
                n_requests += 1
                total_tokens += ntok
                sec = to_seconds(get_col(row, "timestamp") or "", ts_unit)
                if sec is None:
                    n_no_ts += 1
                    continue
                bucket = int(sec // 60)
                minute_tokens[bucket] = minute_tokens.get(bucket, 0) + ntok

    def stats_for(series: list[int]) -> dict:
        if not series:
            return {"avg": 0.0, "p50": 0.0, "p80": 0.0, "p90": 0.0, "n_minutes": 0}
        s = sorted(series)
        return {
            "avg": sum(series) / len(series),
            "p50": percentile(s, 50),
            "p80": percentile(s, 80),
            "p90": percentile(s, 90),
            "n_minutes": len(series),
        }

    if minute_tokens:
        lo, hi = min(minute_tokens), max(minute_tokens)
        span_series = [minute_tokens.get(b, 0) for b in range(lo, hi + 1)]
        active_series = list(minute_tokens.values())
    else:
        span_series, active_series = [], []

    return {
        "n_requests": n_requests,
        "n_missing_ts": n_no_ts,
        "total_tokens": total_tokens,
        "span": stats_for(span_series),
        "active": stats_for(active_series),
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="每个数据集 每分钟 token 涌入 avg/P50/P80/P90 (cache 压力)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--dir", type=Path, default=Path("data"), help="数据集根目录")
    p.add_argument("--datasets", type=str, default="",
                   help="逗号分隔数据集名 (raw 模式: <dir>/<name>/raw/*.csv; "
                        "converted 模式: 直接给 .csv 文件路径). 空=自动发现")
    p.add_argument("--input-format", choices=["raw", "converted"], default="raw",
                   help="raw=读 raw_prompt 现场分词 (慢, 需 transformers); "
                        "converted=复用 convert_trace.py 产出的 input_length 列 "
                        "(秒级, 不需分词)")
    p.add_argument("--minute-mode", choices=["span", "active"], default="span",
                   help="写进 JSON 顶层(供绘图)的口径: span=含空闲分钟, active=仅有流量分钟")
    p.add_argument("--timestamp-unit", choices=["auto", "s", "ms", "us"], default="auto")
    # encoder (与 target_users_hit_rate.py 对齐)
    p.add_argument("--encoder", type=str, default="glm5_token",
                   choices=["byte", "hf_token", "glm5_token"])
    p.add_argument("--tokenizer-path", type=str, default="models/glm5_tokenizer")
    p.add_argument("--chat-mode", type=str, default="wrap_user")
    p.add_argument("--block-size", type=int, default=128, help="(token 计数不用; 仅 encoder 构造需要)")
    p.add_argument("--json-out", type=Path, default=Path("outputs/cache_pressure/stats.json"))
    p.add_argument("--csv-out", type=Path, default=None, help="可选: 同时写一份扁平 CSV")
    return p.parse_args()


def _clean_name(token: str) -> str:
    """文件路径 → 干净的数据集名 (用于解析 size/day + 图标签)."""
    p = Path(token)
    name = p.stem if p.suffix == ".csv" else p.name
    for suf in ("_4variant", "_converted"):
        if name.endswith(suf):
            name = name[: -len(suf)]
    return name


def main() -> None:
    args = parse_args()
    if args.input_format == "raw":
        from lib.hf_tokenizer import apply_template
        from lib.prompt_encoder import build_encoder_from_args
        encoder, meta = build_encoder_from_args(args)
        if not hasattr(encoder, "tokenizer"):
            raise SystemExit("cache 压力以 token 计, 需要 token 级 encoder "
                             "(--encoder glm5_token / hf_token), 不能用 byte.")
        print(f"Encoder: {meta['name']} (chat_mode={meta['chat_mode']}, "
              f"tokenizer={meta['tokenizer_path']})")

        def count_fn(row: dict) -> int:
            prompt = get_col(row, "raw_prompt") or ""
            return len(apply_template(encoder.tokenizer, prompt, encoder.chat_mode)) \
                if prompt else 0
    else:  # converted: 复用 convert_trace.py 写的 input_length, 不分词
        meta = {"name": "from_converted",
                "note": "token 数复用 convert_trace.py 的 input_length 列 (未重新分词)"}
        print("复用 converted/4variant CSV 的 input_length 列 (不分词).")

        def count_fn(row: dict) -> int:
            v = get_col(row, "input_length")
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return 0

    if args.datasets.strip():
        names = [s.strip() for s in args.datasets.split(",") if s.strip()]
    elif args.input_format == "converted":
        names = [str(p) for p in
                 sorted(args.dir.glob("*_4variant.csv")) + sorted(args.dir.glob("*_converted.csv"))]
    else:
        names = discover_datasets(args.dir)
    if not names:
        raise SystemExit(f"在 {args.dir} 下没找到数据集.")

    rows = []
    print(f"\n{'dataset':<22} {'size':>5} {'day':>6} {'reqs':>9} {'min':>6} "
          f"{'avg':>10} {'P50':>10} {'P80':>10} {'P90':>10}   (口径={args.minute_mode}, tok/min)")
    for token in names:
        csvs = resolve_csvs(token, args.dir)
        name = _clean_name(token)
        size, day, day_label = parse_size_day(name)
        if not csvs:
            print(f"{name:<22} {'—':>5} {'—':>6}  (找不到 CSV, 跳过)")
            continue
        res = analyze_dataset(csvs, count_fn, args.timestamp_unit)
        sel = res[args.minute_mode]
        rows.append({
            "name": name, "size": size, "day": day, "day_label": day_label,
            "n_requests": res["n_requests"], "n_missing_ts": res["n_missing_ts"],
            "total_tokens": res["total_tokens"],
            "minute_mode": args.minute_mode,
            "n_minutes": sel["n_minutes"],
            "avg": sel["avg"], "p50": sel["p50"], "p80": sel["p80"], "p90": sel["p90"],
            "span": res["span"], "active": res["active"],
        })
        print(f"{name:<22} {str(size or '—'):>5} {str(day_label or '—'):>6} "
              f"{res['n_requests']:>9,} {sel['n_minutes']:>6,} "
              f"{sel['avg']:>10,.0f} {sel['p50']:>10,.0f} {sel['p80']:>10,.0f} {sel['p90']:>10,.0f}")

    out = {
        "metric": "tokens_per_minute_influx",
        "minute_mode": args.minute_mode,
        "encoder": meta,
        "datasets": rows,
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\nJSON 已写: {args.json_out}")

    if args.csv_out:
        args.csv_out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv_out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["dataset", "size", "day_label", "n_requests", "n_minutes",
                        "minute_mode", "avg", "p50", "p80", "p90", "total_tokens"])
            for r in rows:
                w.writerow([r["name"], r["size"], r["day_label"], r["n_requests"],
                            r["n_minutes"], r["minute_mode"],
                            f"{r['avg']:.2f}", f"{r['p50']:.2f}",
                            f"{r['p80']:.2f}", f"{r['p90']:.2f}", r["total_tokens"]])
        print(f"CSV 已写: {args.csv_out}")

    print(f"\n下一步绘图 (matplotlib venv):\n"
          f"  .venv/bin/python3 scripts/plot_cache_pressure.py "
          f"--stats {args.json_out} --output-dir {args.json_out.parent} --lang zh")


if __name__ == "__main__":
    main()
