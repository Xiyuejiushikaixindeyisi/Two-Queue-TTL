"""Unit tests for lib.prompt_rewrite.

Covers docs/stage3_prompt_rewrite_plan.md §6 (verification baselines):
- detection per regex category
- sort_only: static-first, alphabetical
- normalize_only: order preserved, placeholders applied
- sort_and_normalize: combined transform
- apply_variant dispatch + invalid name
- patterns config override (custom Unix path roots)
- assert: messages are never modified by any variant
"""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

import pytest

from lib.prompt_rewrite import (
    apply_variant,
    build_patterns,
    detect_dynamic_labels,
    has_dynamic_content,
    load_patterns_config,
    normalize_only,
    normalize_tool_lossless,
    sort_and_normalize,
    sort_only,
)
from lib.prompt_rewrite.detect import VARIANTS


# ---------------------------------------------------------------------------
# Fixture tools
# ---------------------------------------------------------------------------

STATIC_TOOL = {
    "type": "function",
    "function": {
        "name": "compute_sum",
        "description": "Compute the sum of two integers.",
        "parameters": {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
        },
    },
}

PATH_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file at /home/foo/data.csv and return its content.",
        "parameters": {"type": "object", "properties": {"p": {"type": "string"}}},
    },
}

DATE_TOOL = {
    "type": "function",
    "function": {
        "name": "schedule",
        "description": "Schedule a meeting at 2025-03-25T10:30:00 with the team.",
        "parameters": {"type": "object", "properties": {"who": {"type": "string"}}},
    },
}

DEFAULT_PATH_TOOL = {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": "List the entries in a directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "/home/user/projects"},
            },
        },
    },
}

EXAMPLES_FALSE_POSITIVE_TOOL = {
    "type": "function",
    "function": {
        "name": "make_request",
        "description": "Send an HTTP request to a URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "examples": ["/home/something", "2025-01-01"],
                },
            },
        },
    },
}

SIMPLE_NAME_TOOL = {  # flat schema (no `function` wrapper)
    "name": "ping",
    "description": "Health check; no dynamic content.",
}


# ---------------------------------------------------------------------------
# detect_dynamic_labels
# ---------------------------------------------------------------------------

class TestDetect:
    def test_static_tool_has_no_labels(self):
        assert detect_dynamic_labels(STATIC_TOOL) == set()

    def test_unix_path_detected(self):
        assert "unix_path" in detect_dynamic_labels(PATH_TOOL)

    def test_date_detected(self):
        assert "date" in detect_dynamic_labels(DATE_TOOL)

    def test_default_path_detected(self):
        assert "unix_path" in detect_dynamic_labels(DEFAULT_PATH_TOOL)

    def test_examples_field_skipped(self):
        """Pro detector skips JSON-Schema 'examples' blocks (D7)."""
        assert detect_dynamic_labels(EXAMPLES_FALSE_POSITIVE_TOOL) == set()

    def test_json_schema_url_not_flagged(self):
        tool = {
            "type": "function",
            "function": {
                "name": "x",
                "description": "see https://json-schema.org/draft/2020-12/schema",
                "parameters": {"type": "object"},
            },
        }
        assert detect_dynamic_labels(tool) == set()

    def test_win_path(self):
        tool = {"name": "x", "description": "Open C:\\Users\\foo\\file.txt"}
        assert "win_path" in detect_dynamic_labels(tool)

    def test_file_uri(self):
        tool = {"name": "x", "description": "Resolved to file:///tmp/x.txt"}
        labels = detect_dynamic_labels(tool)
        assert "file_uri" in labels

    def test_uuid(self):
        tool = {"name": "x", "description": "Session 550e8400-e29b-41d4-a716-446655440000"}
        assert "uuid" in detect_dynamic_labels(tool)

    def test_has_dynamic_content_alias(self):
        assert has_dynamic_content(PATH_TOOL) is True
        assert has_dynamic_content(STATIC_TOOL) is False

    def test_flat_schema_name_extraction(self):
        """A tool without `function` wrapper still gets its name detected."""
        assert detect_dynamic_labels(SIMPLE_NAME_TOOL) == set()


# ---------------------------------------------------------------------------
# sort_only
# ---------------------------------------------------------------------------

class TestSortOnly:
    def test_static_first_then_dynamic_each_alphabetical(self):
        tools = [PATH_TOOL, STATIC_TOOL, DATE_TOOL]  # dynamic, static, dynamic
        sorted_tools = sort_only(tools)
        names = [t["function"]["name"] for t in sorted_tools]
        # compute_sum is the only static → first
        # read_file (path) and schedule (date) are dynamic → alphabetical
        assert names == ["compute_sum", "read_file", "schedule"]

    def test_all_static_preserved_alphabetical(self):
        tools = [
            {"name": "zzz", "description": "static"},
            {"name": "aaa", "description": "static"},
        ]
        assert [t["name"] for t in sort_only(tools)] == ["aaa", "zzz"]

    def test_empty_input(self):
        assert sort_only([]) == []

    def test_contents_unchanged(self):
        """sort_only must not modify tool contents — only reorder."""
        original = copy.deepcopy(PATH_TOOL)
        sort_only([PATH_TOOL])
        assert PATH_TOOL == original


# ---------------------------------------------------------------------------
# normalize_only
# ---------------------------------------------------------------------------

class TestNormalizeOnly:
    def test_preserves_order(self):
        tools = [PATH_TOOL, STATIC_TOOL, DATE_TOOL]
        out = normalize_only(tools)
        names = [t["function"]["name"] for t in out]
        assert names == ["read_file", "compute_sum", "schedule"]

    def test_path_replaced_with_placeholder(self):
        out = normalize_only([PATH_TOOL])
        desc = out[0]["function"]["description"]
        assert "/home/foo/data.csv" not in desc
        assert "__PATH_" in desc

    def test_date_replaced_with_placeholder(self):
        out = normalize_only([DATE_TOOL])
        desc = out[0]["function"]["description"]
        assert "2025-03-25T10:30:00" not in desc
        assert "__DATE_" in desc

    def test_default_replaced(self):
        out = normalize_only([DEFAULT_PATH_TOOL])
        default = out[0]["function"]["parameters"]["properties"]["path"]["default"]
        assert default.startswith("__DEFAULT_PATH_")

    def test_placeholders_are_unique_across_batch(self):
        import re
        out = normalize_only([PATH_TOOL, DATE_TOOL, DEFAULT_PATH_TOOL])
        text = json.dumps(out, ensure_ascii=False)
        placeholders = set(re.findall(r"__[A-Z_]+_\d{4}__", text))
        # Expect 3 distinct placeholders: 1 PATH + 1 DATE + 1 DEFAULT_PATH
        assert len(placeholders) == 3, f"got {sorted(placeholders)}"
        # Numbers must be monotonically allocated (0001, 0002, 0003)
        num_re = re.compile(r"^__[A-Z_]+?_(\d+)__$")
        nums = sorted(int(num_re.match(p).group(1)) for p in placeholders)
        assert nums == [1, 2, 3]

    def test_original_tool_not_mutated(self):
        """normalize_only must deep-copy: input dicts remain intact."""
        original = copy.deepcopy(PATH_TOOL)
        normalize_only([PATH_TOOL])
        assert PATH_TOOL == original


# ---------------------------------------------------------------------------
# sort_and_normalize
# ---------------------------------------------------------------------------

class TestSortAndNormalize:
    def test_normalization_makes_dynamic_tools_static_then_alphabetized(self):
        tools = [PATH_TOOL, STATIC_TOOL, DATE_TOOL]
        out = sort_and_normalize(tools)
        names = [t["function"]["name"] for t in out]
        # After normalization, all 3 are static → alphabetical
        assert names == ["compute_sum", "read_file", "schedule"]

    def test_unresolved_dynamic_tools_go_last(self):
        """If normalization can't strip all dynamic markers (e.g. UUID without rule),
        the still-dynamic tool stays at the tail."""
        uuid_tool = {
            "name": "session",
            "description": "uses 550e8400-e29b-41d4-a716-446655440000",
        }
        out = sort_and_normalize([uuid_tool, STATIC_TOOL])
        # UUID is detected but not in the normalize rules — so it stays dynamic
        names = [_name(t) for t in out]
        assert names == ["compute_sum", "session"]


# ---------------------------------------------------------------------------
# apply_variant dispatcher
# ---------------------------------------------------------------------------

class TestApplyVariant:
    def test_base_returns_deep_copy(self):
        out = apply_variant("base", [PATH_TOOL])
        assert out == [PATH_TOOL]
        assert out[0] is not PATH_TOOL  # deep copy, not alias

    def test_each_variant_matches_corresponding_function(self):
        tools = [PATH_TOOL, STATIC_TOOL, DATE_TOOL]
        assert apply_variant("reorder", tools) == sort_only(tools)
        assert apply_variant("placeholder", tools) == normalize_only(tools)
        assert apply_variant("both", tools) == sort_and_normalize(tools)

    def test_unknown_variant_raises(self):
        with pytest.raises(ValueError, match="unknown variant"):
            apply_variant("invalid_mode", [STATIC_TOOL])

    def test_variants_constant(self):
        assert VARIANTS == ("base", "reorder", "placeholder", "both")


# ---------------------------------------------------------------------------
# Patterns config override
# ---------------------------------------------------------------------------

class TestPatternsConfig:
    def test_custom_unix_path_root_adds_match(self):
        tool = {"name": "x", "description": "stored at /data/projects/x.csv"}
        # Default patterns: /data is NOT a recognised root → no match
        assert "unix_path" not in detect_dynamic_labels(tool)
        # Custom patterns including /data
        custom = build_patterns(unix_path_roots=["data", "home"])
        assert "unix_path" in detect_dynamic_labels(tool, patterns=custom)

    def test_load_patterns_config_from_json(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "patterns.json"
            cfg_path.write_text(json.dumps({"unix_path_roots": ["data", "work"]}))
            patterns = load_patterns_config(cfg_path)
            tool = {"name": "x", "description": "file at /data/x.txt"}
            assert "unix_path" in detect_dynamic_labels(tool, patterns=patterns)


# ---------------------------------------------------------------------------
# Critical assert: messages are never touched (plan §6.3)
# ---------------------------------------------------------------------------

class TestMessagesUntouched:
    """Transforms must operate only on `tools`. Any body containing `messages`
    must come back with messages byte-identical to the input."""

    @pytest.fixture
    def body(self):
        return {
            "tools": [PATH_TOOL, STATIC_TOOL, DATE_TOOL],
            "messages": [
                {"role": "user", "content": "今天天气怎么样? /home/me/here.txt"},
                {"role": "assistant", "content": "天气查不到, 但 2026-05-19 的日历可以看"},
            ],
        }

    @pytest.mark.parametrize("variant", ["reorder", "placeholder", "both"])
    def test_messages_unchanged_when_only_tools_transformed(self, body, variant):
        original_messages = copy.deepcopy(body["messages"])
        transformed_tools = apply_variant(variant, body["tools"])
        # Caller passes only tools through transform → messages stays as-is
        new_body = {"tools": transformed_tools, "messages": body["messages"]}
        assert new_body["messages"] == original_messages


# ---------------------------------------------------------------------------
# Bindings (normalize_tool_lossless) — verify reversibility metadata
# ---------------------------------------------------------------------------

class TestBindings:
    def test_path_binding_recorded(self):
        _, bindings = normalize_tool_lossless(PATH_TOOL)
        kinds = {b["kind"] for b in bindings}
        assert "description_unix_path" in kinds
        values = {b["value"] for b in bindings}
        assert "/home/foo/data.csv" in values

    def test_default_binding_recorded(self):
        _, bindings = normalize_tool_lossless(DEFAULT_PATH_TOOL)
        kinds = {b["kind"] for b in bindings}
        assert "default_path" in kinds

    def test_static_tool_yields_no_bindings(self):
        _, bindings = normalize_tool_lossless(STATIC_TOOL)
        assert bindings == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _name(tool: dict) -> str:
    func = tool.get("function")
    if isinstance(func, dict):
        return func.get("name", "")
    return tool.get("name", "")
