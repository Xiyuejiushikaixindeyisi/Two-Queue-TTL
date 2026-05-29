#!/usr/bin/env python3
"""给一个文件夹, 一条命令得到里面每个 CSV 数据集的理想 KV cache 命中率 (token 级).

这是平台最核心的「跨数据集理想命中率对比」入口. 与既有脚本的分工:
- quick_hit_rate.py      : 同样按文件夹/逐 CSV, 但**字节级** (相对 vllm 系统偏高 0-30pp);
- target_users_hit_rate.py: token 级, 但**按 user** 选 top-K + 需要 data/<model>/raw/ 嵌套布局;
- 本脚本 dataset_hit_rate.py: token 级 + 扁平文件夹 + **数据集级 (整库 pooled)** 命中率.

两种形态
--------
形态 1 (整库): 文件夹里每个 *.csv 当成一个数据集, 把该 CSV 内**所有**请求当成
               共享同一个 prefix cache 的请求流 (跨 app/用户的前缀复用都计入,
               最贴近线上单实例 vLLM). 一个 CSV 出一行.

    python3 scripts/dataset_hit_rate.py --dir /mnt/esfs/zhangxiyue/GLM-V5/ \\
        --encoder glm5_token --tokenizer-path models/glm5_tokenizer --chat-mode wrap_user

形态 2 (指定 app-id): 加 --app-id <租户ID>, 每个 CSV 只统计该 app-id 的请求.

    python3 scripts/dataset_hit_rate.py --dir /mnt/esfs/zhangxiyue/GLM-V5/ \\
        --app-id S0012345 \\
        --encoder glm5_token --tokenizer-path models/glm5_tokenizer --chat-mode wrap_user

命中率口径 (理想 / 无淘汰)
-------------------------
ideal_hit_rate = hit_blocks / total_blocks, 其中 hit_blocks = total_blocks - unique_blocks.
因为 block key 是 chain hash (位置敏感的前缀身份), 每个 key 第一次出现必 miss, 之后必 hit,
seen 集合只增不减 → 结果与请求**顺序无关**, 无需按 timestamp 排序.

CSV 列: request_id / user_id / raw_prompt / timestamp, 支持中文别名
        (请求ID / 租户ID / 请求参数) + UTF-8 BOM. app-id 默认匹配 user_id 列,
        可用 --app-col 指定其它列名.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.csv_trace import get_col, iter_rows, parse_ts, resolve_app  # noqa: E402
from lib.hit_rate import lcp  # noqa: E402
from lib.prompt_encoder import build_encoder_from_args, resolve_max_input_tokens  # noqa: E402


def analyze_dataset(
    csv_path: Path,
    encoder,
    block_size: int,
    *,
    app_id: str | None = None,
    app_col: str | None = None,
    max_input_tokens: int | None = None,
) -> dict:
    """单 CSV 的整库 pooled 理想命中率 (可选只看某 app-id).

    max_input_tokens: 超过此 token 数的请求**整条丢弃** (模型放不下), 计入 skipped_over_length,
    不计入 reqs/blocks。None = 不过滤。

    Returns dict: reqs / n_apps / total_blocks / unique_blocks / hit_blocks /
    ideal_hit_rate / trace_duration_min / avg_gb_per_min / skipped_over_length.
    """
    seen: set[bytes] = set()
    hit = 0
    total = 0
    reqs = 0
    skipped_over_length = 0
    apps: set[str] = set()
    earliest: int | None = None
    latest: int | None = None

    for row in iter_rows(csv_path):
        uid = resolve_app(row, app_col)
        if app_id is not None and uid != app_id:
            continue

        prompt = get_col(row, "raw_prompt") or ""
        keys: list[bytes] = []
        if prompt:
            keys, ntok = encoder.encode_with_length(prompt)
            if max_input_tokens is not None and ntok > max_input_tokens:
                skipped_over_length += 1
                continue  # 整条丢弃: 不计入 reqs/apps/ts/blocks

        reqs += 1
        if uid:
            apps.add(uid)
        ts = parse_ts(get_col(row, "timestamp"))
        if ts is not None:
            if earliest is None or ts < earliest:
                earliest = ts
            if latest is None or ts > latest:
                latest = ts
        if not prompt:
            continue

        hit += lcp(keys, seen)
        total += len(keys)
        seen.update(keys)

    unique = len(seen)
    ideal = (hit / total) if total else 0.0
    if earliest is not None and latest is not None and latest >= earliest:
        dur_min = (latest - earliest) / 60.0
    else:
        dur_min = 0.0
    kv_bpt = getattr(encoder, "kv_bytes_per_token", None)
    if kv_bpt is not None and dur_min > 0:
        avg_gb_per_min = unique * block_size * kv_bpt / (1024 ** 3) / dur_min
    else:
        avg_gb_per_min = None

    return {
        "reqs": reqs,
        "n_apps": len(apps),
        "total_blocks": total,
        "unique_blocks": unique,
        "hit_blocks": hit,
        "ideal_hit_rate": ideal,
        "trace_duration_min": round(dur_min, 2),
        "avg_gb_per_min": avg_gb_per_min,
        "skipped_over_length": skipped_over_length,
    }


def discover_csvs(inputs: list[str], dir_: Path | None, glob: str) -> list[Path]:
    """把位置参数 (文件或目录) + --dir 展开成有序去重的 CSV 列表."""
    found: list[Path] = []
    if dir_ is not None:
        found.extend(sorted(dir_.glob(glob)))
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            found.extend(sorted(p.glob(glob)))
        elif p.is_file():
            found.append(p)
        else:
            print(f"warning: {p} 不存在, 跳过", file=sys.stderr)
    # 去重保序
    seen: set[Path] = set()
    out: list[Path] = []
    for p in found:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(p)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("inputs", nargs="*", help="CSV 文件或目录 (与 --dir 二选一或并用)")
    p.add_argument("--dir", type=Path, default=None, help="数据集文件夹, glob 里面的 *.csv")
    p.add_argument("--glob", type=str, default="*.csv",
                   help="--dir / 目录参数下的匹配模式 (默认 *.csv; 递归用 '**/*.csv')")
    p.add_argument("--app-id", type=str, default=None,
                   help="形态 2: 只统计该 app-id (默认匹配 user_id/租户ID 列) 的请求")
    p.add_argument("--app-col", type=str, default=None,
                   help="app-id 所在列名 (默认 user_id/租户ID); 数据里另有 app 列时用")
    # encoder (与 target_users_hit_rate.py / per_user_report_analyzer.py 对齐)
    p.add_argument("--encoder", type=str, default="glm5_token",
                   choices=["byte", "glm5_token", "hf_token"],
                   help="编码策略 (默认 glm5_token = token 级, vllm 对齐; byte = 字节级基线)")
    p.add_argument("--tokenizer-path", type=str, default="models/glm5_tokenizer",
                   help="HF tokenizer 目录 (glm5_token / hf_token 用); 例如 models/qwen_v3_tokenizer")
    p.add_argument("--chat-mode", type=str, default="wrap_user",
                   choices=["raw", "wrap_user", "messages"], help="chat template 包裹方式")
    p.add_argument("--block-size", type=int, default=128)
    p.add_argument("--max-input-tokens", type=int, default=None,
                   help="超过此 token 数的请求整条丢弃 (默认自动取 tokenizer.model_max_length)")
    p.add_argument("--no-length-filter", action="store_true",
                   help="关闭超长过滤 (默认开: 用 tokenizer 上限)")
    p.add_argument("--csv-out", type=Path, default=None, help="(可选) 把表写到 CSV")
    args = p.parse_args(argv)

    csv_files = discover_csvs(args.inputs, args.dir, args.glob)
    if not csv_files:
        print("error: 没找到任何 CSV (用位置参数给文件/目录, 或 --dir)", file=sys.stderr)
        return 1

    encoder, encoder_meta = build_encoder_from_args(args)
    block_size = getattr(encoder, "block_size_tokens",
                         getattr(encoder, "block_size_bytes", args.block_size))
    max_tokens = resolve_max_input_tokens(encoder, args.max_input_tokens, args.no_length_filter)

    mode = f"app-id={args.app_id}" if args.app_id is not None else "整库 pooled"
    print(f"Encoder: {encoder_meta['name']} (block_size={args.block_size} {encoder_meta['block_unit']}, "
          f"chat_mode={encoder_meta['chat_mode']}, "
          f"kv_bytes_per_token={encoder_meta.get('kv_bytes_per_token')})")
    print(f"Mode: {mode}; datasets: {len(csv_files)}; "
          f"max_input_tokens={max_tokens if max_tokens is not None else '不过滤'}")
    if encoder_meta["block_unit"] == "bytes":
        print("⚠ 字节级: ideal_hit_rate 相对 vllm 系统性偏高 0-30pp (见 docs/metrics_glossary.md §3)")
    print()

    rows: list[tuple[str, dict]] = []
    for csv_path in csv_files:
        name = csv_path.stem
        t0 = time.time()
        print(f"[{name}] analyzing... ", end="", flush=True)
        try:
            stats = analyze_dataset(csv_path, encoder, block_size,
                                    app_id=args.app_id, app_col=args.app_col,
                                    max_input_tokens=max_tokens)
            over = stats["skipped_over_length"]
            over_note = f", 丢弃超长 {over:,}" if over else ""
            print(f"done in {time.time() - t0:.1f}s  ({stats['reqs']:,} reqs{over_note})")
            rows.append((name, stats))
        except Exception as e:  # noqa: BLE001
            print(f"❌ {e}", file=sys.stderr)

    if not rows:
        print("error: 所有数据集都失败了", file=sys.stderr)
        return 1

    # ===== Terminal table =====
    width = 118
    print(f"\n{'=' * width}")
    print(f"Dataset ideal hit rate — {encoder_meta['name']}; {mode}")
    print(f"GB/min = unique × {block_size} × kv_bytes_per_token / 1024³ / duration_min")
    print(f"{'=' * width}")
    hdr = (f"{'dataset':<34} {'reqs':>9} {'apps':>6} {'total_blk':>12} "
           f"{'uniq_blk':>12} {'hit_rate':>10} {'GB/min':>10} {'dur':>9}")
    print(hdr)
    print("-" * width)
    for name, v in rows:
        gb_cell = f"{v['avg_gb_per_min']:.4f}" if v["avg_gb_per_min"] is not None else "—"
        dur_cell = f"{v['trace_duration_min']:.1f}m"
        nm = (name[:32] + "..") if len(name) > 34 else name
        print(f"{nm:<34} {v['reqs']:>9,} {v['n_apps']:>6,} {v['total_blocks']:>12,} "
              f"{v['unique_blocks']:>12,} {v['ideal_hit_rate']:>10.4f} {gb_cell:>10} {dur_cell:>9}")

    if args.csv_out is not None:
        args.csv_out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv_out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["dataset", "app_id_filter", "reqs", "n_apps", "total_blocks",
                        "unique_blocks", "hit_blocks", "ideal_hit_rate",
                        "trace_duration_min", "avg_gb_per_min"])
            for name, v in rows:
                gb_str = f"{v['avg_gb_per_min']:.6f}" if v["avg_gb_per_min"] is not None else ""
                w.writerow([name, args.app_id or "", v["reqs"], v["n_apps"],
                            v["total_blocks"], v["unique_blocks"], v["hit_blocks"],
                            f"{v['ideal_hit_rate']:.6f}", f"{v['trace_duration_min']:.2f}", gb_str])
        print(f"\nCSV → {args.csv_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
