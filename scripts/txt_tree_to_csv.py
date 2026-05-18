#!/usr/bin/env python3
"""把 txt 树形数据集转成 pipeline 兼容的 4 列 CSV.

输入布局 (两种均支持, 自动检测)
--------------------------------
    flat (input-dir 直接放 txt):
        <input-dir>/
          <request_id_1>.txt   内容 = raw_prompt
          <request_id_2>.txt
    nested (按 subdir 分组):
        <input-dir>/
          <subdir1>/
            <request_id_1>.txt
          <subdir2>/ …
检测规则: input-dir 直接有 *.txt → flat 模式 (subdir_name 取 input-dir basename);
        否则递归子目录.

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
    """字典序遍历 txt 文件 → (subdir_name, txt_path).

    两种布局自动检测:
    - flat:   <input-dir>/*.txt          → subdir_name = input_dir.name
    - nested: <input-dir>/<sub>/*.txt   → subdir_name = sub
    """
    direct_txts = sorted(input_dir.glob("*.txt"))
    if direct_txts:
        for txt in direct_txts:
            yield input_dir.name, txt
        return
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
    p.add_argument("--request-id-mode", type=str, default="sequential",
                   choices=["sequential", "filename"],
                   help="request_id 生成方式 (默认 sequential = r-000001/r-000002/...; "
                        "filename = txt 文件名去 .txt, 中文文件名时不推荐)")
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
    # Prompt 长度统计 (chars / utf-8 bytes), 用于阶段 2 快速看数据规模
    max_prompt_chars = 0
    max_prompt_bytes = 0
    max_prompt_file: str | None = None
    total_prompt_bytes = 0

    with open(args.output_csv, "w", encoding="utf-8", newline="") as fout:
        writer = csv.writer(fout)
        writer.writerow(["request_id", "user_id", "raw_prompt", "timestamp"])

        for seq_idx, (subdir_name, txt_path) in enumerate(iter_txt_files(args.input_dir), 1):
            if args.request_id_mode == "sequential":
                request_id = f"r-{seq_idx:06d}"
            else:  # filename
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
            else:
                n_chars = len(raw_prompt)
                n_bytes = len(raw_prompt.encode("utf-8"))
                total_prompt_bytes += n_bytes
                if n_chars > max_prompt_chars:
                    max_prompt_chars = n_chars
                    max_prompt_bytes = n_bytes
                    max_prompt_file = txt_path.name

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
    print(f"rid mode:     {args.request_id_mode}")
    print(f"rows written: {rows_written}")
    print(f"empty files:  {empty_files}")
    print(f"decode errs:  {decode_errors}")
    if max_prompt_chars > 0:
        avg_bytes = total_prompt_bytes / max(1, rows_written - empty_files)
        print(f"max prompt:   {max_prompt_chars:,} chars / {max_prompt_bytes:,} bytes  "
              f"({max_prompt_file!r})")
        print(f"avg prompt:   {avg_bytes:,.0f} bytes  (total {total_prompt_bytes:,} bytes / "
              f"{rows_written - empty_files:,} non-empty rows)")
    if duplicate_request_ids:
        print(f"\n⚠ duplicate request_ids: {len(duplicate_request_ids)} "
              f"(same file stem across subdirs — CSV 内会有重名 rid; "
              f"用 --request-id-mode sequential 可避免)")
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
