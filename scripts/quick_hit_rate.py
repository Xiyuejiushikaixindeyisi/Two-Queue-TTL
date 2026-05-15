#!/usr/bin/env python3
"""批量算 CSV 数据集的字节级理想 KV cache 命中率 (轻量版).

设计目标: 拿到一批新 CSV 数据时, 快速 sanity check ideal_hit_rate 数字,
不跑完整 v2 pipeline (无 chain forest / 无 HTML / 无时序图).

CSV 格式要求: 4 列 (request_id / user_id / raw_prompt / timestamp),
              支持中文列名 (请求ID / 租户ID / 请求参数) + UTF-8 BOM.

输出: 命令行表格, 每行一个 dataset, 含 users / blocks / ideal_hit_rate.
--per-user 模式额外打印每 user 详情.

Usage:
    python3 scripts/quick_hit_rate.py data1.csv data2.csv data3.csv
    python3 scripts/quick_hit_rate.py --dir data/new_traces/
    python3 scripts/quick_hit_rate.py *.csv --per-user
    python3 scripts/quick_hit_rate.py trace.csv --block-size 256

依赖: 仅 Python 3.8+ 标准库 (hashlib / csv / argparse / pathlib / collections).
      可拷贝到任何机器跑, 不需要 repo 其他文件.

注: ideal_hit_rate 是字节级估计, 相对 vllm 实际命中率系统性偏高 0-30pp.
    长 prompt 业务 (≥ 500 tokens) 虚高 < 5pp; 短 prompt 业务最高 30pp.
    详见 docs/metrics_glossary.md §3.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)

# 支持中文列名 (生产 trace 常见)
ALIASES = {
    "请求ID":   "request_id",
    "租户ID":   "user_id",
    "请求参数": "raw_prompt",
}


def get_col(row: dict, target: str) -> str | None:
    """支持中文列名 / UTF-8 BOM 的列读取."""
    for k, v in ALIASES.items():
        if v == target and k in row:
            return row[k]
    return row.get(target)


def analyze(csv_path: Path, block_size: int) -> dict:
    """Single-pass: 同时算 model-level (跨 user) + per-user LCP hit_rate.

    Returns dict with model_hit / total_blocks / n_users / per_user.
    """
    g_seen: set[bytes] = set()
    u_seen: dict[str, set[bytes]] = defaultdict(set)
    g_hit = 0
    g_total = 0
    u_hit: dict[str, int] = defaultdict(int)
    u_total: dict[str, int] = defaultdict(int)
    u_req: dict[str, int] = defaultdict(int)

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            uid = get_col(row, "user_id") or "<unknown>"
            prompt = get_col(row, "raw_prompt") or ""
            u_req[uid] += 1
            if not prompt:
                continue

            # block 切分 (utf-8 bytes) + SHA256 hash chain
            encoded = prompt.encode("utf-8")
            blocks = [encoded[i:i + block_size]
                      for i in range(0, len(encoded), block_size)]
            keys: list[bytes] = []
            prev = b""
            for b in blocks:
                prev = hashlib.sha256(prev + b).digest()
                keys.append(prev)

            g_total += len(keys)
            u_total[uid] += len(keys)

            # LCP: 哈希链性质 → 连续 hit 直到第一个 miss
            g_lcp = 0
            for k in keys:
                if k in g_seen:
                    g_lcp += 1
                else:
                    break
            us = u_seen[uid]
            u_lcp = 0
            for k in keys:
                if k in us:
                    u_lcp += 1
                else:
                    break

            g_hit += g_lcp
            u_hit[uid] += u_lcp
            for k in keys:
                g_seen.add(k)
                us.add(k)

    return {
        "model_hit":    g_hit / g_total if g_total else 0.0,
        "total_blocks": g_total,
        "n_users":      len(u_total),
        "per_user": {
            uid: {
                "hit_rate": u_hit[uid] / u_total[uid],
                "blocks":   u_total[uid],
                "reqs":     u_req[uid],
            }
            for uid in sorted(u_total, key=lambda x: -u_total[x])
            if u_total[uid] > 0
        },
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("paths", nargs="*", type=Path,
                   help="CSV 文件路径列表")
    p.add_argument("--dir", type=Path, default=None,
                   help="目录, 递归扫所有 *.csv (与 paths 互补)")
    p.add_argument("--block-size", type=int, default=128,
                   help="block 字节大小 (默认 128, 与现有 v2 工具一致)")
    p.add_argument("--per-user", action="store_true",
                   help="额外打印每 user hit_rate")
    args = p.parse_args()

    files = list(args.paths)
    if args.dir:
        files.extend(sorted(args.dir.rglob("*.csv")))
    if not files:
        print("usage: quick_hit_rate.py file.csv [...] 或 --dir <path>",
              file=sys.stderr)
        sys.exit(1)

    print(f"\n{'#':>3} {'dataset':<60} {'users':>6} {'blocks':>12} {'ideal_hit':>10}")
    print("=" * 100)
    results = []
    for i, f in enumerate(files, 1):
        try:
            r = analyze(f, args.block_size)
            results.append((f, r))
            print(f"{i:>3} {f.name[:60]:<60} {r['n_users']:>6} "
                  f"{r['total_blocks']:>12,} {r['model_hit']:>10.4f}")
        except Exception as e:
            print(f"{i:>3} {str(f)[:60]:<60} ❌ {e}", file=sys.stderr)

    if not results:
        sys.exit(1)

    if len(results) > 1:
        avg = sum(r["model_hit"] for _, r in results) / len(results)
        total_blocks = sum(r["total_blocks"] for _, r in results)
        print(f"\n汇总: {len(results)} dataset, "
              f"平均 ideal_hit={avg:.4f}, 总 blocks={total_blocks:,}")

    if args.per_user:
        print("\nPer-user 详情:")
        for f, r in results:
            if not r["per_user"]:
                continue
            print(f"\n[{f.name}]")
            for uid, d in r["per_user"].items():
                print(f"  {uid[:50]:<50} hit={d['hit_rate']:>7.4f}  "
                      f"blocks={d['blocks']:>12,}  reqs={d['reqs']:>7,}")

    print(f"\n注: 字节级 ideal_hit_rate, block_size={args.block_size} bytes")
    print(f"    相对 vllm 实际命中率偏高 0-30pp (短 prompt 业务更严重).")
    print(f"    详见 docs/metrics_glossary.md §3.")


if __name__ == "__main__":
    main()
