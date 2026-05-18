#!/usr/bin/env python3
"""跨数据集 hit_rate + GB/min 快速筛选 (无 HTML, terminal+CSV+MD 表).

用途
----
KV cache 离线分析平台 funnel 阶段 1: 跨多个数据集 (同 tokenizer 一批)
快速看每个 (model, user) 的 ideal_hit_rate + 平均 GB/min, 用于选定值得
进阶段 3 (出 HTML 详细分析) 的"高研究价值"场景.

两种 user 选择模式
------------------
- **auto-top-k** (默认): 不传 --users 时, 自动选每个数据集请求量 top-4
  的 user (--auto-top-k 可改 N). 不同 model 的 top-k 列表可以不同 →
  输出 long 表 (每行一个 (model, user)).
- **fixed**: --users uid1,uid2,... 显式给定一组 user (跨 model 共用),
  输出 long 表 + Markdown pivot 表 (model × user).

输出 4 个核心指标
-----------------
1. ideal_hit_rate (token 级与 vllm 一致, 无淘汰上限)
2. avg_gb_per_min (token 模式才有; byte 模式显 "—"):
     unique_blocks × block_size × kv_bytes_per_token / 1024³ / user_duration_min
3. trace_duration_min (per-user, 用于解读 GB/min 是否代表全量)
4. unique_blocks (用于推算当天总 GB)

CSV 格式: 4 列 (request_id / user_id / raw_prompt / timestamp),
          支持中文别名 (请求ID / 租户ID / 请求参数) + UTF-8 BOM.

Usage
-----
    # 默认 auto-top-4 + Qwen3 tokenizer
    python3 scripts/target_users_hit_rate.py --dir data \\
      --models GLM-V5-32K-0513,GLM-V5-128K-0513 \\
      --encoder glm5_token --tokenizer-path models/glm5_tokenizer

    # 指定具体 user 跨数据集对比 (pivot 表)
    python3 scripts/target_users_hit_rate.py --dir data \\
      --models GLM-V5-32K-0513 \\
      --users S00...3734,com.huawei... \\
      --encoder glm5_token --tokenizer-path models/glm5_tokenizer

    # 改 top-N (默认 4)
    python3 scripts/target_users_hit_rate.py --dir data \\
      --auto-top-k 10 --encoder hf_token --tokenizer-path models/qwen_v3_tokenizer

Output
------
    1. terminal: long 表 (行=model+rank+user, 列=reqs/hit_rate/GB-min/duration)
    2. CSV:      1 row per (model, user) — 9 列
    3. MD:       完整字段 + pivot (仅 --users 模式)
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

# Step 1.6: lib/ for PromptEncoder strategies
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.prompt_encoder import build_encoder_from_args  # noqa: E402

csv.field_size_limit(sys.maxsize)


ALIASES = {
    "请求ID":   "request_id",
    "租户ID":   "user_id",
    "请求参数": "raw_prompt",
}


def get_col(row: dict, target: str) -> str | None:
    """支持中文列名 / UTF-8 BOM 的列读取。"""
    for k, v in ALIASES.items():
        if v == target and k in row:
            return row[k]
    return row.get(target)


def count_users_in_csvs(csv_files: list[Path]) -> dict[str, int]:
    """Pass-0 (轻量): 只读 user_id 列, 数 per-user 请求数. 用于 auto-top-k 选举."""
    counts: dict[str, int] = {}
    for csv_path in csv_files:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                uid = get_col(row, "user_id") or ""
                if not uid:
                    continue
                counts[uid] = counts.get(uid, 0) + 1
    return counts


def analyze_one_model(
    csv_files: list[Path], target_users: list[str], encoder,
    block_size: int,
) -> dict:
    """对一个 model 的 CSV, 算 target user 的 hit_rate + GB/min.

    Returns {uid: stats_dict_or_None}. None 表示该 user 在此 model 无 record.
    `block_size` 是 encoder.block_size_tokens (token 模式) 或 .block_size_bytes
    (byte 模式), 调用方负责取对.
    """
    target_set = set(target_users)
    per_seen: dict[str, set[bytes]] = {uid: set() for uid in target_users}
    per_hit:      dict[str, int]      = {uid: 0 for uid in target_users}
    per_total:    dict[str, int]      = {uid: 0 for uid in target_users}
    per_reqs:     dict[str, int]      = {uid: 0 for uid in target_users}
    per_earliest: dict[str, int | None] = {uid: None for uid in target_users}
    per_latest:   dict[str, int | None] = {uid: None for uid in target_users}

    for csv_path in csv_files:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                uid = get_col(row, "user_id")
                if uid not in target_set:
                    continue
                per_reqs[uid] += 1

                # timestamp (用于 trace_duration_min → 平均 GB/min)
                ts_raw = get_col(row, "timestamp") or ""
                try:
                    ts = int(float(ts_raw))
                    if per_earliest[uid] is None or ts < per_earliest[uid]:
                        per_earliest[uid] = ts
                    if per_latest[uid] is None or ts > per_latest[uid]:
                        per_latest[uid] = ts
                except (ValueError, TypeError):
                    pass  # graceful: 缺 ts 不影响 hit_rate

                prompt = get_col(row, "raw_prompt") or ""
                if not prompt:
                    continue

                keys = encoder.encode(prompt)
                seen = per_seen[uid]

                # LCP: hash-chain property → 连续 hit 直到第一个 miss 就停
                lcp = 0
                for k in keys:
                    if k in seen:
                        lcp += 1
                    else:
                        break
                per_hit[uid] += lcp
                per_total[uid] += len(keys)
                for k in keys:
                    seen.add(k)

    result: dict[str, dict | None] = {}
    kv_bpt = getattr(encoder, "kv_bytes_per_token", None)  # None for byte mode
    for uid in target_users:
        if per_reqs[uid] == 0:
            result[uid] = None
            continue
        t = per_total[uid]
        ub = len(per_seen[uid])
        if (per_earliest[uid] is not None and per_latest[uid] is not None
                and per_latest[uid] >= per_earliest[uid]):
            dur_min = (per_latest[uid] - per_earliest[uid]) / 60.0
        else:
            dur_min = 0.0
        # 平均 GB/min: total unique block KV bytes / duration
        # (token 模式才填, byte 模式无 KV cache 概念)
        if kv_bpt is not None and dur_min > 0:
            avg_gb_per_min = ub * block_size * kv_bpt / (1024 ** 3) / dur_min
        else:
            avg_gb_per_min = None
        result[uid] = {
            "reqs":               per_reqs[uid],
            "total_blocks":       t,
            "unique_blocks":      ub,
            "hit_blocks":         per_hit[uid],
            "ideal_hit_rate":     (per_hit[uid] / t) if t else 0.0,
            "trace_duration_min": round(dur_min, 2),
            "avg_gb_per_min":     avg_gb_per_min,  # None when byte/no-ts
        }
    return result


def discover_models(data_dir: Path) -> list[tuple[str, list[Path]]]:
    """自动发现 data/<model>/raw/*.csv 布局。

    跳过 data/out_*, data/models, data/*_tokenizer 等非数据集目录。
    注意: 此函数会把 data/ 下**所有**含 raw/*.csv 的子目录都返回; 如需
    限定子集 (例如只算本次新数据集而排除历史 trace), 请显式传 --models。
    """
    models = []
    for sub in sorted(data_dir.iterdir()):
        if not sub.is_dir():
            continue
        name = sub.name
        if name.startswith("out_") or name.startswith(".") or name.endswith("_tokenizer"):
            continue
        raw_dir = sub / "raw"
        if not raw_dir.is_dir():
            continue
        csvs = sorted(raw_dir.glob("*.csv"))
        if csvs:
            models.append((name, csvs))
    return models


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dir", type=Path, default=Path("data"),
                   help="基础数据目录, 含 <model>/raw/*.csv (默认: data/)")
    p.add_argument("--models", type=str, default="",
                   help="逗号分隔 model 目录名 (默认: 自动 discover)")
    p.add_argument("--users", type=str, default="",
                   help="逗号分隔目标 user_id (空 = auto-top-k 模式, 默认: 每数据集取请求量 top-N)")
    p.add_argument("--auto-top-k", type=int, default=4,
                   help="--users 留空时, 每数据集自动选请求量 top-N user (默认 4)")
    # encoder 选项 (与 per_user_report_analyzer.py 对齐)
    p.add_argument("--encoder", type=str, default="byte",
                   choices=["byte", "glm5_token", "hf_token"],
                   help="编码策略 (默认 byte; glm5_token = 兼容 alias 指向 models/glm5_tokenizer; "
                        "hf_token = 任意 HF tokenizer, 由 --tokenizer-path 指定)")
    p.add_argument("--tokenizer-path", type=str, default="models/glm5_tokenizer",
                   help="HF tokenizer 目录或 HF repo id (glm5_token / hf_token 用); "
                        "例如 models/qwen_v3_tokenizer, models/qwen_v35_tokenizer")
    p.add_argument("--chat-mode", type=str, default="wrap_user",
                   choices=["raw", "wrap_user", "messages"],
                   help="chat template wrapping (默认 wrap_user)")
    p.add_argument("--block-size", type=int, default=128)
    # 输出
    p.add_argument("--csv-out", type=Path, default=Path("target_users_hit_rate.csv"),
                   help="输出 CSV 路径")
    p.add_argument("--md-out", type=Path, default=Path("target_users_hit_rate.md"),
                   help="输出 Markdown 路径")
    args = p.parse_args()

    # User selection mode
    fixed_users = [u.strip() for u in args.users.split(",") if u.strip()]
    auto_mode = len(fixed_users) == 0

    # Resolve model list
    if args.models:
        model_names = [m.strip() for m in args.models.split(",") if m.strip()]
        models: list[tuple[str, list[Path]]] = []
        for name in model_names:
            raw_dir = args.dir / name / "raw"
            if not raw_dir.is_dir():
                print(f"warning: {raw_dir} not found, skipping", file=sys.stderr)
                continue
            csvs = sorted(raw_dir.glob("*.csv"))
            if csvs:
                models.append((name, csvs))
            else:
                print(f"warning: no *.csv under {raw_dir}, skipping", file=sys.stderr)
    else:
        models = discover_models(args.dir)

    if not models:
        print(f"error: no models found under {args.dir}", file=sys.stderr)
        sys.exit(1)

    encoder, encoder_meta = build_encoder_from_args(args)
    # block_size used for GB/min math: token mode uses block_size_tokens
    block_size = getattr(encoder, "block_size_tokens",
                         getattr(encoder, "block_size_bytes", args.block_size))

    print(f"Encoder: {encoder_meta['name']} (block_size={args.block_size} {encoder_meta['block_unit']}, "
          f"chat_mode={encoder_meta['chat_mode']}, "
          f"kv_bytes_per_token={encoder_meta.get('kv_bytes_per_token')})")
    if auto_mode:
        print(f"User mode: auto-top-{args.auto_top_k} (per dataset)")
    else:
        print(f"User mode: fixed list ({len(fixed_users)} users)")
        for uid in fixed_users:
            print(f"  • {uid}")
    print(f"Models ({len(models)}):")
    for name, csvs in models:
        print(f"  • {name}  ({len(csvs)} csv)")
    print()

    # ===== Per-model resolve target users + analyze =====
    # results[model] = ordered list of (uid, stats_dict | None), ordered by rank
    results: dict[str, list[tuple[str, dict | None]]] = {}
    for model_name, csvs in models:
        t0 = time.time()
        # Resolve target user list
        if auto_mode:
            print(f"[{model_name}] pass-0 user count... ", end="", flush=True)
            counts = count_users_in_csvs(csvs)
            targets = sorted(counts, key=lambda u: counts[u], reverse=True)[:args.auto_top_k]
            print(f"top-{args.auto_top_k}: {[f'{u[:18]}…({counts[u]:,})' for u in targets[:4]]}...")
        else:
            targets = fixed_users

        print(f"[{model_name}] analyzing {len(targets)} users... ", end="", flush=True)
        try:
            res = analyze_one_model(csvs, targets, encoder, block_size)
            elapsed = time.time() - t0
            n_seen = sum(1 for v in res.values() if v is not None)
            print(f"done in {elapsed:.1f}s  ({n_seen}/{len(targets)} present)")
            results[model_name] = [(uid, res[uid]) for uid in targets]
        except Exception as e:
            print(f"❌ {e}", file=sys.stderr)
            results[model_name] = [(uid, None) for uid in targets]

    # ===== Terminal: long-format table per model =====
    print(f"\n{'=' * 110}")
    print(f"Long table — {encoder_meta['name']}; GB/min = unique × {block_size} × kv_bytes_per_token / 1024³ / duration_min")
    print(f"{'=' * 110}")
    print(f"{'model':<28} {'rank':>4} {'user_id':<36} {'reqs':>8} {'hit_rate':>10} {'GB/min':>10} {'duration':>10}")
    print("-" * 110)
    for model_name, rows in results.items():
        for rank, (uid, v) in enumerate(rows, 1):
            uid_short = (uid[:34] + "..") if len(uid) > 36 else uid
            if v is None:
                print(f"{model_name[:28]:<28} {rank:>4} {uid_short:<36} {'—':>8} {'—':>10} {'—':>10} {'—':>10}")
                continue
            gb_cell = f"{v['avg_gb_per_min']:.4f}" if v["avg_gb_per_min"] is not None else "—"
            dur_cell = f"{v['trace_duration_min']:.1f}m"
            print(f"{model_name[:28]:<28} {rank:>4} {uid_short:<36} "
                  f"{v['reqs']:>8,} {v['ideal_hit_rate']:>10.4f} {gb_cell:>10} {dur_cell:>10}")

    # ===== CSV (long format, all 9 columns) =====
    args.csv_out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.csv_out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["model", "rank", "user_id", "reqs", "total_blocks",
                    "unique_blocks", "hit_blocks", "ideal_hit_rate",
                    "trace_duration_min", "avg_gb_per_min"])
        for model_name, rows in results.items():
            for rank, (uid, v) in enumerate(rows, 1):
                if v is None:
                    w.writerow([model_name, rank, uid, 0, 0, 0, 0, "", "", ""])
                else:
                    gb_str = f"{v['avg_gb_per_min']:.6f}" if v["avg_gb_per_min"] is not None else ""
                    w.writerow([model_name, rank, uid,
                                v["reqs"], v["total_blocks"],
                                v["unique_blocks"], v["hit_blocks"],
                                f"{v['ideal_hit_rate']:.6f}",
                                f"{v['trace_duration_min']:.2f}",
                                gb_str])
    print(f"\nCSV → {args.csv_out}")

    # ===== Markdown =====
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.md_out, "w", encoding="utf-8") as f:
        f.write(f"# Cross-dataset hit_rate + GB/min ({encoder_meta['name']})\n\n")
        f.write(f"- Encoder: `{encoder_meta['name']}`, block_size = {args.block_size} {encoder_meta['block_unit']}\n")
        if encoder_meta["chat_mode"]:
            f.write(f"- chat_mode: `{encoder_meta['chat_mode']}`\n")
            f.write(f"- tokenizer: `{encoder_meta['tokenizer_path']}`\n")
            kvb = encoder_meta.get("kv_bytes_per_token")
            if kvb:
                f.write(f"- kv_bytes_per_token: `{kvb:,}` (用于 GB/min 估算)\n")
        f.write(f"- User mode: " + ("auto-top-" + str(args.auto_top_k) + " per dataset"
                                   if auto_mode else f"fixed {len(fixed_users)} users") + "\n")

        # Long table (universal — works for both modes)
        f.write("\n## Long table\n\n")
        f.write("| model | rank | user_id | reqs | total_blocks | unique_blocks | hit_blocks | ideal_hit_rate | duration_min | avg_GB/min |\n")
        f.write("|---|---:|---|---:|---:|---:|---:|---:|---:|---:|\n")
        for model_name, rows in results.items():
            for rank, (uid, v) in enumerate(rows, 1):
                if v is None:
                    f.write(f"| {model_name} | {rank} | `{uid}` | 0 | — | — | — | — | — | — |\n")
                    continue
                gb_cell = f"**{v['avg_gb_per_min']:.4f}**" if v["avg_gb_per_min"] is not None else "—"
                f.write(f"| {model_name} | {rank} | `{uid}` | {v['reqs']:,} | "
                        f"{v['total_blocks']:,} | {v['unique_blocks']:,} | "
                        f"{v['hit_blocks']:,} | **{v['ideal_hit_rate']:.4f}** | "
                        f"{v['trace_duration_min']:.1f} | {gb_cell} |\n")

        # Pivot — only meaningful when --users is fixed (same user list across models)
        if not auto_mode and fixed_users:
            f.write("\n## Pivot: ideal_hit_rate (reqs) — fixed user list\n\n")
            short_headers = [(f"…{u[-12:]}" if len(u) > 12 else u) for u in fixed_users]
            f.write("| model | " + " | ".join(short_headers) + " |\n")
            f.write("|---|" + "---|" * len(fixed_users) + "\n")
            for model_name, rows in results.items():
                cells = [model_name]
                per_uid = {uid: v for uid, v in rows}
                for uid in fixed_users:
                    v = per_uid.get(uid)
                    if v is None:
                        cells.append("—")
                    else:
                        cells.append(f"**{v['ideal_hit_rate']:.4f}** ({v['reqs']:,})")
                f.write("| " + " | ".join(cells) + " |\n")

    print(f"MD  → {args.md_out}")


if __name__ == "__main__":
    main()
