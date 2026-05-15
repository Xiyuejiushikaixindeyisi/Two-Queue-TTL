#!/usr/bin/env python3
"""跨数据集追踪固定 4 个 user_id 的 ideal_hit_rate (Step 1.6 P4 辅助工具).

为什么需要这个脚本
----------------
per_user_report_analyzer.py 默认 --top-k=3 + --min-request-pct=0.01, 只
分析每个数据集请求量前 3 的用户。如果你要追踪的 4 个用户里有人在某个
数据集占比 < 1%, 它就会被 excluded, 拿不到 user_report.json。

这个脚本绕过 top-K 筛选, 直接从 raw CSV 算每个目标 user 的 ideal_hit_rate。

默认 4 个目标 user (--users 可覆盖):
  S00000000000000000000000000000961
  S00000000000000000000000000003734
  com.huawei.fin.bfd.lgrp
  fcc54a9a2ef64bf0a26016389681550e

CSV 格式要求: 4 列 (request_id / user_id / raw_prompt / timestamp),
              支持中文别名 (请求ID / 租户ID / 请求参数) + UTF-8 BOM.

Usage
-----
    # 自动扫所有 data/<model>/raw/*.csv (推荐, 一行命令)
    python3 scripts/target_users_hit_rate.py --dir data/ --encoder glm5_token

    # 指定 model 子集
    python3 scripts/target_users_hit_rate.py --dir data/ \\
        --models GLM-V5-32K-0513,GLM-V5-8K-0513 --encoder glm5_token

    # 字节级 (regression baseline)
    python3 scripts/target_users_hit_rate.py --dir data/

    # 自定义 users
    python3 scripts/target_users_hit_rate.py --dir data/ \\
        --users uid1,uid2,uid3 --encoder glm5_token

Output
------
    1. 终端: pivot 表 (行=model, 列=user, cell=ideal_hit_rate(reqs))
    2. CSV:  target_users_hit_rate.csv     (1 row per (model, user))
    3. MD:   target_users_hit_rate.md      (pivot + 完整字段)
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


DEFAULT_USERS = [
    "S00000000000000000000000000000961",
    "S00000000000000000000000000003734",
    "com.huawei.fin.bfd.lgrp",
    "fcc54a9a2ef64bf0a26016389681550e",
]

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


def analyze_one_model(csv_files: list[Path], target_users: list[str], encoder) -> dict:
    """Single-pass: 对一个 model 的 CSV, 算 4 个目标 user 各自的 hit_rate。

    Returns {uid: stats_dict_or_None}. None 表示该 user 在此 model 无 record。
    """
    target_set = set(target_users)
    per_seen: dict[str, set[bytes]] = {uid: set() for uid in target_users}
    per_hit:   dict[str, int]      = {uid: 0 for uid in target_users}
    per_total: dict[str, int]      = {uid: 0 for uid in target_users}
    per_reqs:  dict[str, int]      = {uid: 0 for uid in target_users}

    for csv_path in csv_files:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                uid = get_col(row, "user_id")
                if uid not in target_set:
                    continue
                per_reqs[uid] += 1
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
    for uid in target_users:
        if per_reqs[uid] == 0:
            result[uid] = None
        else:
            t = per_total[uid]
            result[uid] = {
                "reqs": per_reqs[uid],
                "total_blocks": t,
                "unique_blocks": len(per_seen[uid]),
                "hit_blocks": per_hit[uid],
                "ideal_hit_rate": (per_hit[uid] / t) if t else 0.0,
            }
    return result


def discover_models(data_dir: Path) -> list[tuple[str, list[Path]]]:
    """自动发现 data/<model>/raw/*.csv 布局。

    跳过 data/out_*, data/models, data/glm5_tokenizer 等非数据集目录。
    """
    models = []
    for sub in sorted(data_dir.iterdir()):
        if not sub.is_dir():
            continue
        name = sub.name
        if name.startswith("out_") or name.startswith(".") or name == "glm5_tokenizer":
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
    p.add_argument("--users", type=str, default=",".join(DEFAULT_USERS),
                   help="逗号分隔目标 user_id (默认: 4 个内嵌)")
    # encoder 选项 (与 per_user_report_analyzer.py 对齐)
    p.add_argument("--encoder", type=str, default="byte",
                   choices=["byte", "glm5_token"],
                   help="编码策略 (默认 byte; glm5_token 需 transformers + tokenizer)")
    p.add_argument("--tokenizer-path", type=str, default="models/glm5_tokenizer",
                   help="GLM-5 tokenizer 路径 (仅 --encoder=glm5_token 时用)")
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

    target_users = [u.strip() for u in args.users.split(",") if u.strip()]

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
    print(f"Encoder: {encoder_meta['name']} (block_size={args.block_size} {encoder_meta['block_unit']}, "
          f"chat_mode={encoder_meta['chat_mode']})")
    print(f"Target users ({len(target_users)}):")
    for uid in target_users:
        print(f"  • {uid}")
    print(f"Models ({len(models)}):")
    for name, csvs in models:
        print(f"  • {name}  ({len(csvs)} csv)")
    print()

    # Run analysis
    results: dict[str, dict] = {}
    for model_name, csvs in models:
        t0 = time.time()
        print(f"[{model_name}] analyzing... ", end="", flush=True)
        try:
            results[model_name] = analyze_one_model(csvs, target_users, encoder)
            elapsed = time.time() - t0
            n_seen = sum(1 for v in results[model_name].values() if v is not None)
            print(f"done in {elapsed:.1f}s  ({n_seen}/{len(target_users)} target users present)")
        except Exception as e:
            print(f"❌ {e}", file=sys.stderr)
            results[model_name] = {uid: None for uid in target_users}

    # ===== 终端 pivot 表 =====
    print(f"\n{'=' * 110}")
    print(f"Pivot: ideal_hit_rate (request count) — {encoder_meta['name']}")
    print(f"{'=' * 110}")
    col_w = 22
    header = f"{'model':<32}" + "".join(f"{uid[:col_w-2]:>{col_w}}" for uid in target_users)
    print(header)
    print("-" * (32 + col_w * len(target_users)))
    for model_name, per_user in results.items():
        row = f"{model_name[:32]:<32}"
        for uid in target_users:
            v = per_user.get(uid)
            if v is None:
                cell = "—"
            else:
                cell = f"{v['ideal_hit_rate']:.4f}({v['reqs']:,})"
            row += f"{cell:>{col_w}}"
        print(row)

    # ===== CSV =====
    args.csv_out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.csv_out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["model", "user_id", "reqs", "total_blocks",
                    "unique_blocks", "hit_blocks", "ideal_hit_rate"])
        for model_name, per_user in results.items():
            for uid in target_users:
                v = per_user.get(uid)
                if v is None:
                    w.writerow([model_name, uid, 0, 0, 0, 0, ""])
                else:
                    w.writerow([model_name, uid,
                                v["reqs"], v["total_blocks"],
                                v["unique_blocks"], v["hit_blocks"],
                                f"{v['ideal_hit_rate']:.6f}"])
    print(f"\nCSV → {args.csv_out}")

    # ===== Markdown =====
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.md_out, "w", encoding="utf-8") as f:
        f.write(f"# Target Users ideal_hit_rate ({encoder_meta['name']})\n\n")
        f.write(f"- Encoder: `{encoder_meta['name']}`, block_size = {args.block_size} {encoder_meta['block_unit']}\n")
        if encoder_meta["chat_mode"]:
            f.write(f"- chat_mode: `{encoder_meta['chat_mode']}`\n")
            f.write(f"- tokenizer: `{encoder_meta['tokenizer_path']}`\n")
        f.write("\n## 目标用户\n\n")
        for uid in target_users:
            f.write(f"- `{uid}`\n")

        # Pivot
        f.write("\n## Pivot: ideal_hit_rate (reqs)\n\n")
        short_headers = []
        for uid in target_users:
            short = uid[-12:] if len(uid) > 12 else uid
            short_headers.append(f"…{short}" if len(uid) > 12 else short)
        f.write("| model | " + " | ".join(short_headers) + " |\n")
        f.write("|---|" + "---|" * len(target_users) + "\n")
        for model_name, per_user in results.items():
            cells = [model_name]
            for uid in target_users:
                v = per_user.get(uid)
                if v is None:
                    cells.append("—")
                else:
                    cells.append(f"**{v['ideal_hit_rate']:.4f}** ({v['reqs']:,})")
            f.write("| " + " | ".join(cells) + " |\n")

        # Full stats
        f.write("\n## 完整字段\n\n")
        f.write("| model | user_id | reqs | total_blocks | unique_blocks | hit_blocks | ideal_hit |\n")
        f.write("|---|---|---:|---:|---:|---:|---:|\n")
        for model_name, per_user in results.items():
            for uid in target_users:
                v = per_user.get(uid)
                if v is None:
                    f.write(f"| {model_name} | `{uid}` | 0 | — | — | — | — |\n")
                else:
                    f.write(f"| {model_name} | `{uid}` | {v['reqs']:,} | "
                            f"{v['total_blocks']:,} | {v['unique_blocks']:,} | "
                            f"{v['hit_blocks']:,} | **{v['ideal_hit_rate']:.4f}** |\n")
    print(f"MD  → {args.md_out}")


if __name__ == "__main__":
    main()
