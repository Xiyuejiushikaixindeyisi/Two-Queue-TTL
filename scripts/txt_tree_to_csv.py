#!/usr/bin/env python3
"""把 txt 树形数据集转成 pipeline 兼容的 4 列 CSV.

输入布局
--------
    <input-dir>/
      <subdir1>/
        <request_id_1>.txt    内容 = raw_prompt (UTF-8, 默认)
        <request_id_2>.txt
      <subdir2>/ …
      <subdir3>/ …

输出 CSV (4 列, 列名与 per_user_report_analyzer / target_users_hit_rate 一致):
    request_id, user_id, raw_prompt, timestamp

字段约定
--------
- request_id : txt 文件名去掉 .txt 后缀
- user_id    : 见 --user-id-from
- raw_prompt : txt 文件全文 (CSV writer 自动 quote, 换行/逗号都保留)
- timestamp  : **固定填空字符串**. 下游 pipeline 会 fallback 到 0,
               导致 spike detection / time-series / rpm 都失效但 hit_rate /
               LCP / chain forest 不受影响 (timestamp 不参与这几项计算).

user_id 映射 (--user-id-from)
-----------------------------
- subdir   (默认): 用子文件夹名当 user_id (3 subdir → 3 个 user)
- fixed         : 全部统一为 --fixed-user-id 指定的字符串
- map-json      : --user-map 指向 JSON 文件 {"subdir1":"u1","subdir2":"u1","subdir3":"u2"}
                  允许多对一合并, 或重命名为更友好的 user_id

Usage
-----
    # 默认: subdir 名作 user_id
    python3 scripts/txt_tree_to_csv.py \\
      --input-dir /path/to/Qwen-V3-8B \\
      --output-csv data/Qwen3-8B-txt/raw/converted.csv

    # 全部并入 1 个 user
    python3 scripts/txt_tree_to_csv.py \\
      --input-dir /path/to/Qwen-V3-8B \\
      --output-csv data/Qwen3-8B-txt/raw/converted.csv \\
      --user-id-from fixed --fixed-user-id all

    # 自定义 subdir → user_id 映射
    echo '{"subdir1":"user_a","subdir2":"user_a","subdir3":"user_b"}' > /tmp/user_map.json
    python3 scripts/txt_tree_to_csv.py \\
      --input-dir /path/to/Qwen-V3-8B \\
      --output-csv data/Qwen3-8B-txt/raw/converted.csv \\
      --user-id-from map-json --user-map /tmp/user_map.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

csv.field_size_limit(sys.maxsize)


def derive_user_id(
    subdir_name: str,
    mode: str,
    fixed_user_id: str | None,
    user_map: dict[str, str] | None,
) -> str:
    if mode == "subdir":
        return subdir_name
    if mode == "fixed":
        if not fixed_user_id:
            raise ValueError("--user-id-from=fixed requires --fixed-user-id")
        return fixed_user_id
    if mode == "map-json":
        if not user_map:
            raise ValueError("--user-id-from=map-json requires --user-map JSON")
        if subdir_name not in user_map:
            raise KeyError(
                f"subdir {subdir_name!r} not in user_map "
                f"(keys: {sorted(user_map.keys())})"
            )
        return user_map[subdir_name]
    raise ValueError(f"unknown --user-id-from mode: {mode!r}")


def iter_txt_files(input_dir: Path):
    """字典序遍历 <input-dir>/<subdir>/*.txt → (subdir_name, txt_path)."""
    for subdir in sorted(input_dir.iterdir()):
        if not subdir.is_dir():
            continue
        for txt in sorted(subdir.glob("*.txt")):
            yield subdir.name, txt


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--input-dir", type=Path, required=True,
                   help="顶层目录 (含 <subdir>/<request_id>.txt)")
    p.add_argument("--output-csv", type=Path, required=True,
                   help="输出 CSV 路径 (建议 data/<model>/raw/converted.csv)")
    p.add_argument("--user-id-from", type=str, default="subdir",
                   choices=["subdir", "fixed", "map-json"],
                   help="user_id 字段来源 (默认: subdir 名)")
    p.add_argument("--fixed-user-id", type=str, default=None,
                   help="--user-id-from=fixed 时写入 user_id 列的字符串")
    p.add_argument("--user-map", type=Path, default=None,
                   help="--user-id-from=map-json 时, 指向 JSON 文件: {subdir: user_id}")
    p.add_argument("--encoding", type=str, default="utf-8",
                   help="txt 文件编码 (默认 utf-8)")
    args = p.parse_args()

    if not args.input_dir.is_dir():
        print(f"error: --input-dir {args.input_dir} not a directory", file=sys.stderr)
        sys.exit(1)

    user_map: dict[str, str] | None = None
    if args.user_id_from == "map-json":
        if not args.user_map or not args.user_map.is_file():
            print("error: --user-id-from=map-json requires --user-map <file>",
                  file=sys.stderr)
            sys.exit(1)
        with open(args.user_map, encoding="utf-8") as f:
            user_map = json.load(f)

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)

    rows_written = 0
    per_user: dict[str, int] = {}
    per_subdir: dict[str, int] = {}
    empty_files = 0
    decode_errors = 0
    seen_request_ids: dict[str, str] = {}  # request_id → first-seen subdir
    duplicate_request_ids: list[tuple[str, str, str]] = []  # (rid, first_sub, dup_sub)

    with open(args.output_csv, "w", encoding="utf-8", newline="") as fout:
        writer = csv.writer(fout)
        writer.writerow(["request_id", "user_id", "raw_prompt", "timestamp"])

        for subdir_name, txt_path in iter_txt_files(args.input_dir):
            request_id = txt_path.stem
            try:
                raw_prompt = txt_path.read_text(encoding=args.encoding)
            except UnicodeDecodeError as e:
                decode_errors += 1
                print(f"warn: decode error in {txt_path}: {e}; skipping",
                      file=sys.stderr)
                continue
            if not raw_prompt:
                empty_files += 1

            if request_id in seen_request_ids:
                duplicate_request_ids.append(
                    (request_id, seen_request_ids[request_id], subdir_name)
                )
            else:
                seen_request_ids[request_id] = subdir_name

            try:
                user_id = derive_user_id(
                    subdir_name, args.user_id_from, args.fixed_user_id, user_map
                )
            except (ValueError, KeyError) as e:
                print(f"error: {e}", file=sys.stderr)
                sys.exit(1)

            writer.writerow([request_id, user_id, raw_prompt, ""])
            rows_written += 1
            per_user[user_id] = per_user.get(user_id, 0) + 1
            per_subdir[subdir_name] = per_subdir.get(subdir_name, 0) + 1

    print("\n=== conversion summary ===")
    print(f"input:        {args.input_dir}")
    print(f"output:       {args.output_csv}")
    print(f"rows written: {rows_written}")
    print(f"empty files:  {empty_files}")
    print(f"decode errs:  {decode_errors}")
    if duplicate_request_ids:
        print(f"\n⚠ duplicate request_ids: {len(duplicate_request_ids)} "
              f"(same file stem across subdirs — CSV 内会有重名 rid)")
        for rid, sub_a, sub_b in duplicate_request_ids[:5]:
            print(f"    {rid}: first in {sub_a!r}, again in {sub_b!r}")
        if len(duplicate_request_ids) > 5:
            print(f"    … 还有 {len(duplicate_request_ids) - 5} 条")
    print("\nper-subdir:")
    for k in sorted(per_subdir):
        print(f"  {k}: {per_subdir[k]}")
    print("\nper-user_id:")
    for k in sorted(per_user):
        print(f"  {k}: {per_user[k]}")

    # 给出下游一行命令 hint (假设 output 走 data/<model>/raw/*.csv 约定)
    out = args.output_csv.resolve()
    parts = out.parts
    if "raw" in parts:
        idx = parts.index("raw")
        data_dir = Path(*parts[:idx - 1])
        model_dir_name = parts[idx - 1]
        print("\n下一步 (Qwen3 tokenizer 已 vendor):")
        print("  PYTHONPATH=. .venv_glm5/bin/python3 scripts/v2_run_pipeline.py \\")
        print(f"    --data-dir {data_dir} --output-dir outputs \\")
        print(f"    --models {model_dir_name} \\")
        print("    --encoder hf_token --tokenizer-path models/qwen_v3_tokenizer \\")
        print("    --chat-mode wrap_user")


if __name__ == "__main__":
    main()
