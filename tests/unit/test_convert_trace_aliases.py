"""Regression tests for convert_trace.py column-name aliasing.

Production CSVs use Chinese column names with a BOM-prefixed first column and
a leading space after each comma in the header. Without these tests, every
new chat-mode trace from a 中文 CSV would silently produce 0 rows (we hit
exactly that bug on first real-data run, 2026-05-19).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from convert_trace import (  # noqa: E402
    _CHAT_REQUEST_INPUT_ALIASES,
    _CHAT_TS_ALIASES,
    _CHAT_USER_ALIASES,
    _first_present,
    _normalize_header_keys,
)


class TestNormalizeHeaderKeys:
    def test_strips_bom_from_first_column(self):
        row = {"﻿请求ID": "r1", "租户ID": "t1"}
        out = _normalize_header_keys(row)
        assert "请求ID" in out
        assert "﻿请求ID" not in out

    def test_strips_leading_trailing_whitespace(self):
        row = {" 租户ID": "t1", "请求参数 ": "json", "  timestamp  ": "0"}
        out = _normalize_header_keys(row)
        assert out == {"租户ID": "t1", "请求参数": "json", "timestamp": "0"}

    def test_preserves_values(self):
        row = {" 请求参数": ' {"tools": []}  ', "x": "y"}
        out = _normalize_header_keys(row)
        # Values are untouched — only keys are normalized
        assert out["请求参数"] == ' {"tools": []}  '

    def test_handles_empty_row(self):
        assert _normalize_header_keys({}) == {}


class TestFirstPresent:
    def test_returns_first_nonempty_match(self):
        row = {"request_input": "", "请求参数": "json_blob"}
        assert _first_present(row, _CHAT_REQUEST_INPUT_ALIASES) == "json_blob"

    def test_returns_empty_when_nothing_matches(self):
        row = {"other_col": "x"}
        assert _first_present(row, _CHAT_REQUEST_INPUT_ALIASES) == ""

    def test_prefers_earlier_alias(self):
        row = {"request_input": "first", "请求参数": "second"}
        assert _first_present(row, _CHAT_REQUEST_INPUT_ALIASES) == "first"


class TestAliasCompleteness:
    """The 3 alias tuples must cover the production CSV column-name space."""

    def test_request_input_aliases_include_chinese(self):
        assert "请求参数" in _CHAT_REQUEST_INPUT_ALIASES
        assert "request_input" in _CHAT_REQUEST_INPUT_ALIASES

    def test_user_id_aliases_include_chinese(self):
        assert "租户ID" in _CHAT_USER_ALIASES
        assert "user_id" in _CHAT_USER_ALIASES

    def test_timestamp_aliases_cover_common_names(self):
        for name in ("timestamp", "create_time"):
            assert name in _CHAT_TS_ALIASES


class TestEndToEndChineseAliasParsing:
    """A row formatted like production traces must be parseable through the
    chat-mode alias chain: normalize header → look up by alias → get JSON.
    """

    def test_chinese_alias_row_yields_request_input(self):
        # Simulate what DictReader yields for a row from a BOM-prefixed,
        # leading-space-header CSV.
        raw_row = {
            "﻿请求ID": "abc123",
            " 租户ID":      "tenant1",
            " 请求参数":    '{"model":"glm5","messages":[{"role":"user","content":"hi"}],"tools":[]}',
            " timestamp":   "0",
        }
        normalized = _normalize_header_keys(raw_row)
        ri = _first_present(normalized, _CHAT_REQUEST_INPUT_ALIASES)
        assert ri.startswith('{"model"')

        uid = _first_present(normalized, _CHAT_USER_ALIASES)
        assert uid == "tenant1"

        ts = _first_present(normalized, _CHAT_TS_ALIASES)
        assert ts == "0"
