"""Tests for lib/csv_trace.py (canonical CSV trace parsing)."""
from __future__ import annotations

from lib.csv_trace import (
    first_present,
    get_col,
    iter_rows,
    normalize_header_keys,
    parse_ts,
    resolve_app,
)


def test_normalize_strips_bom_and_whitespace():
    row = {"﻿请求ID": "1", " 租户ID": "u", "请求参数 ": "p"}
    n = normalize_header_keys(row)
    assert set(n) == {"请求ID", "租户ID", "请求参数"}


def test_get_col_chinese_and_english():
    n = normalize_header_keys({"请求ID": "1", "租户ID": "u", "请求参数": "body"})
    assert get_col(n, "request_id") == "1"
    assert get_col(n, "user_id") == "u"
    assert get_col(n, "raw_prompt") == "body"
    assert get_col({"user_id": "x"}, "user_id") == "x"
    assert get_col({"user_id": "x"}, "timestamp") is None


def test_first_present():
    row = {"a": "", "b": None, "c": "hit", "d": "later"}
    assert first_present(row, ("a", "b", "c", "d")) == "hit"
    assert first_present(row, ("a", "b")) is None


def test_resolve_app_default_and_custom_col():
    n = normalize_header_keys({"租户ID": "tenantA", "biz": "bizX"})
    assert resolve_app(n) == "tenantA"
    assert resolve_app(n, app_col="biz") == "bizX"
    assert resolve_app({"foo": "bar"}) == ""


def test_parse_ts():
    assert parse_ts("0") == 0
    assert parse_ts("1700000000") == 1700000000
    assert parse_ts("12.0") == 12
    assert parse_ts("") is None
    assert parse_ts(None) is None
    assert parse_ts("abc") is None


def test_iter_rows_handles_chinese_bom_and_leading_space(tmp_path):
    # Simulate a production CSV: BOM on first header, leading space after comma.
    p = tmp_path / "prod.csv"
    p.write_bytes("﻿请求ID, 租户ID, 请求参数, timestamp\n1,tenantA,hello,0\n".encode())
    rows = list(iter_rows(p))
    assert len(rows) == 1
    r = rows[0]
    assert get_col(r, "user_id") == "tenantA"
    assert get_col(r, "raw_prompt") == "hello"
    assert parse_ts(get_col(r, "timestamp")) == 0
