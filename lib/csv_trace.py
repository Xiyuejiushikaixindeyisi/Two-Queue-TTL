"""Canonical CSV trace parsing — single source of truth.

生产 trace CSV 的列名是中文, 首列带 UTF-8 BOM, 且逗号后常有前导空格 (` 租户ID`)。
此前 dataset_hit_rate / model_report / app_report / convert_trace / target_users_hit_rate
各自实现一套解析, 容易口径漂移。统一到这里, 脚本只 import 不重写。

字段约定 (与既有脚本一致):
- 中文列名 → 规范名: 请求ID→request_id, 租户ID→user_id, 请求参数→raw_prompt
- app-id 默认取 user_id/租户ID 列 (可用 app_col 覆盖)
- chat JSON 请求体列名候选见 REQUEST_INPUT_ALIASES
"""
from __future__ import annotations

import csv
import sys
from collections.abc import Iterator
from pathlib import Path

# 生产 prompt 单元格可达数百 KB
csv.field_size_limit(sys.maxsize)

ALIASES = {"请求ID": "request_id", "租户ID": "user_id", "请求参数": "raw_prompt"}

USER_ALIASES = ("user_id", "租户ID")
TS_ALIASES = ("timestamp", "create_time", "ts", "time")
# chat 模式下结构化 JSON 请求体 ({"tools":[...],"messages":[...]}) 所在列
REQUEST_INPUT_ALIASES = ("raw_prompt", "request_input", "请求参数", "request_params")

_BOM = "﻿"


def normalize_header_keys(row: dict) -> dict:
    """去掉列名里的 BOM + 前后空白 (生产 CSV ` 租户ID` 这种前导空格)。"""
    return {(k or "").lstrip(_BOM).strip(): v for k, v in row.items()}


def get_col(row: dict, target: str) -> str | None:
    """按规范名读列, 支持中文别名。传入的 row 应已 normalize_header_keys。"""
    for zh, norm in ALIASES.items():
        if norm == target and zh in row:
            return row[zh]
    return row.get(target)


def first_present(row: dict, keys) -> str | None:
    """返回 keys 中第一个非空值 (用于多别名字段)。"""
    for k in keys:
        v = row.get(k)
        if v:
            return v
    return None


def resolve_app(row: dict, app_col: str | None = None) -> str:
    """取一行的 app-id: app_col 指定列, 否则 user_id/租户ID 列; 缺失返回空串。"""
    if app_col:
        return row.get(app_col) or ""
    return get_col(row, "user_id") or ""


def parse_ts(raw) -> int | None:
    """timestamp → int 秒; 解析失败 (空/非数字) 返回 None (graceful)。"""
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return None


def iter_rows(csv_path: str | Path) -> Iterator[dict]:
    """打开 CSV (utf-8-sig), 逐行产出 header 已规范化的 dict。"""
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            yield normalize_header_keys(row)
