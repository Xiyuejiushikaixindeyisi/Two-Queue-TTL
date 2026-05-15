#!/usr/bin/env python3
"""抽样每个 CSV 数据集的 raw_prompt 前 N 行, 用于判定 chat_mode (Step 1.6 Phase 1).

设计目标: 在 Ascend dev 机上跑, 把输出贴回对话, 让 Claude 判定:
- raw_prompt 是否已含 chat 标记 (<|user|>, [INST], <sop>, ...) → chat_mode=raw
- 是否是 JSON-encoded messages list                          → chat_mode=messages
- 还是纯文本                                                 → chat_mode=wrap_user

CSV 格式要求: 与 quick_hit_rate.py 一致 (request_id / user_id / raw_prompt / timestamp,
              支持中文别名 + UTF-8 BOM).

Usage:
    # 单文件
    python3 scripts/sample_raw_prompts.py /data/M1/raw/M1.csv

    # 一次扫所有数据集 (推荐)
    python3 scripts/sample_raw_prompts.py --dir /data/

    # 多看几条
    python3 scripts/sample_raw_prompts.py --dir /data/ --sample 5

依赖: 仅 Python 3.8+ 标准库.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

csv.field_size_limit(sys.maxsize)

ALIASES = {
    "请求ID":   "request_id",
    "租户ID":   "user_id",
    "请求参数": "raw_prompt",
}

# 已知 chat 标记 (扩展时直接加)
CHAT_MARKERS = [
    ("GLM-5",     [r"\[gMASK\]", r"<sop>",       r"<\|user\|>", r"<\|assistant\|>", r"<\|system\|>"]),
    ("GLM-4",     [r"<\|user\|>", r"<\|assistant\|>"]),
    ("Llama",     [r"\[INST\]",  r"\[/INST\]",   r"<<SYS>>"]),
    ("Qwen/ChatML", [r"<\|im_start\|>", r"<\|im_end\|>"]),
    ("Claude",    [r"\\n\\nHuman:", r"\\n\\nAssistant:"]),
    ("OpenAI-JSON", [r'^\s*\[\s*\{\s*"role"']),
]


def get_col(row: dict, target: str) -> str | None:
    for k, v in ALIASES.items():
        if v == target and k in row:
            return row[k]
    return row.get(target)


def detect_markers(raw: str) -> list[str]:
    """返回命中的 marker family 列表 (e.g. ['GLM-5', 'OpenAI-JSON'])"""
    hits = []
    for family, patterns in CHAT_MARKERS:
        if any(re.search(p, raw) for p in patterns):
            hits.append(family)
    return hits


def looks_like_json_messages(raw: str) -> bool:
    """raw 是否是 JSON-encoded messages list 的形式"""
    s = raw.strip()
    if not (s.startswith("[") and s.endswith("]")):
        return False
    return '"role"' in s and '"content"' in s


def analyze_one(csv_path: Path, sample: int) -> None:
    print(f"\n{'='*90}")
    print(f"Dataset: {csv_path}")
    print(f"{'='*90}")

    # 先统计 total rows + 用 marker stats 看整体
    marker_counter: dict[str, int] = {}
    json_count = 0
    total_seen = 0
    samples: list[tuple[int, str, str]] = []  # (idx, uid, raw)

    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            for i, row in enumerate(csv.DictReader(f)):
                raw = get_col(row, "raw_prompt") or ""
                uid = get_col(row, "user_id") or "?"
                total_seen += 1
                # 统计 marker
                hits = detect_markers(raw)
                for h in hits:
                    marker_counter[h] = marker_counter.get(h, 0) + 1
                if looks_like_json_messages(raw):
                    json_count += 1
                # 取前 N 个不为空的样本
                if len(samples) < sample and raw.strip():
                    samples.append((i, uid, raw))
    except Exception as e:
        print(f"[ERROR] {e}")
        return

    # 全文件统计
    print(f"\n[Stats] total rows = {total_seen:,}")
    print(f"[Stats] JSON-messages-like = {json_count} ({json_count*100/max(total_seen,1):.1f}%)")
    if marker_counter:
        print(f"[Stats] Chat marker hits:")
        for family, cnt in sorted(marker_counter.items(), key=lambda x: -x[1]):
            print(f"  {family:<20} {cnt:>8} ({cnt*100/max(total_seen,1):.1f}%)")
    else:
        print(f"[Stats] No chat markers detected — likely raw user text.")

    # 样本
    print(f"\n[Samples] (first {len(samples)} non-empty)")
    for idx, uid, raw in samples:
        print(f"\n--- row {idx}, user_id={uid[:30]}, len={len(raw)} bytes ({len(raw.encode('utf-8'))} utf-8 bytes) ---")
        # 防止超长 prompt 把屏幕灌满, 显示前 400 + 后 100
        if len(raw) > 600:
            print(raw[:400])
            print(f"\n  ... [truncated {len(raw)-500} chars] ...\n")
            print(raw[-100:])
        else:
            print(raw)
        print(f"--- end row {idx} ---")

    # 判定建议
    print(f"\n[Suggested chat_mode]")
    if json_count > total_seen * 0.5:
        print(f"  → messages  (>50% rows look like JSON messages list)")
    elif marker_counter:
        most = max(marker_counter.items(), key=lambda x: x[1])
        if most[1] > total_seen * 0.5:
            print(f"  → raw       (>50% rows have {most[0]} chat markers — already formatted)")
        else:
            print(f"  → mixed     ({most[0]} hits {most[1]} rows < 50% — may need per-user logic)")
    else:
        print(f"  → wrap_user (no chat markers, plain user text)")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("paths", nargs="*", type=Path, help="CSV 文件列表")
    p.add_argument("--dir", type=Path, default=None, help="目录 (递归扫 *.csv)")
    p.add_argument("--sample", type=int, default=3, help="每数据集采样行数 (默认 3)")
    args = p.parse_args()

    files = list(args.paths)
    if args.dir:
        files.extend(sorted(args.dir.rglob("*.csv")))
    if not files:
        print("usage: sample_raw_prompts.py file.csv [...] 或 --dir <path>", file=sys.stderr)
        sys.exit(1)

    for f in files:
        analyze_one(f, args.sample)

    print(f"\n{'='*90}")
    print("把以上输出 (或脱敏后的版本) 贴回对话, 用于判定 chat_mode.")
    print(f"{'='*90}")


if __name__ == "__main__":
    main()
