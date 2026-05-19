"""Detect and rewrite dynamic content in tool definitions.

Ported from upstream demo `detect_dynamic_tools.py` with two changes:

1. Patterns are passed in (defaulting to `DEFAULT_PATTERNS`), so deployments
   can override the Unix path root list via JSON config.
2. The "basic" detector (which produced false positives from JSON-Schema
   `examples` blocks) is not exposed — only the "pro" detector is used.
   See `docs/stage3_prompt_rewrite_plan.md` decision D7.

Public API
----------
- detect_dynamic_labels(tool, patterns=...)
- has_dynamic_content(tool, patterns=...)
- normalize_tool_lossless(tool, allocator=None, tool_index=None, patterns=...)
- sort_only(tools, patterns=...)               # reorder=on,  placeholder=off
- normalize_only(tools, patterns=...)          # reorder=off, placeholder=on
- sort_and_normalize(tools, patterns=...)      # reorder=on,  placeholder=on  (= demo promax)
- apply_variant(name, tools, patterns=...)     # dispatcher: base/reorder/placeholder/both
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from lib.prompt_rewrite.patterns import DEFAULT_PATTERNS, Patterns

VARIANTS = ("base", "reorder", "placeholder", "both")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _get_tool_name(tool: dict) -> str:
    if not isinstance(tool, dict):
        return ""
    func = tool.get("function")
    if isinstance(func, dict):
        return func.get("name", "")
    return tool.get("name", "")


def _walk_scannable_strings(obj: Any) -> Iterable[str]:
    """Yield every leaf string in a tool definition, skipping `examples` blocks.

    `examples` arrays in JSON-Schema property blocks are developer-authored
    boilerplate, not per-request dynamic content — skipping them eliminates the
    main source of false positives.
    """
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if key == "examples":
                continue
            yield from _walk_scannable_strings(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_scannable_strings(item)


def _deep_copy_json(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _deep_copy_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_copy_json(v) for v in obj]
    return obj


# ── Detection ───────────────────────────────────────────────────────────────

def detect_dynamic_labels(tool: dict, patterns: Patterns = DEFAULT_PATTERNS) -> set[str]:
    """Return the set of matched dynamic-pattern labels for a tool."""
    chunks = list(_walk_scannable_strings(tool))
    blob = "\n".join(chunks)
    blob = patterns.static_url.sub("", blob)
    return {
        label for label, pattern in patterns.dynamic_labelled()
        if pattern.search(blob)
    }


def has_dynamic_content(tool: dict, patterns: Patterns = DEFAULT_PATTERNS) -> bool:
    return bool(detect_dynamic_labels(tool, patterns=patterns))


# ── Placeholder allocator ───────────────────────────────────────────────────

class _PlaceholderAllocator:
    """Monotonic placeholder allocator shared across a tool batch."""

    def __init__(self, start: int = 1):
        self._counter = start - 1

    def new(self, prefix: str) -> str:
        self._counter += 1
        return f"__{prefix}_{self._counter:04d}__"


# ── JSON Pointer helpers (used to record binding paths) ─────────────────────

def _json_pointer_escape(part: str) -> str:
    return str(part).replace("~", "~0").replace("/", "~1")


def _json_pointer_join(base: str, part: str) -> str:
    token = _json_pointer_escape(part)
    if not base:
        return "/" + token
    return base + "/" + token


# ── Lossless normalization ──────────────────────────────────────────────────

def _classify_default_string(value: Any, patterns: Patterns) -> tuple[str, str] | None:
    """Classify a `default` string for normalization. Returns (prefix, kind) or None."""
    if not isinstance(value, str):
        return None
    cleaned = patterns.static_url.sub("", value)
    # Path / file classification before date, so a path with a dated folder
    # is still normalized as a path.
    if patterns.file_uri.search(cleaned):
        return ("DEFAULT_FILE_URI", "default_file_uri")
    if patterns.win_path.search(cleaned) or patterns.unix_path.search(cleaned):
        return ("DEFAULT_PATH", "default_path")
    if patterns.date.search(value):
        return ("DEFAULT_DATE", "default_date")
    return None


def _normalize_text_with_bindings(
    text: str,
    *,
    patterns: Patterns,
    tool_name: str,
    tool_index: int | None,
    path: str,
    allocator: _PlaceholderAllocator,
    bindings: list[dict],
) -> str:
    """Replace dynamic fragments in a description string with placeholders."""
    if not isinstance(text, str) or not text:
        return text

    def _sub(text_in: str, pattern, prefix: str, kind: str) -> str:
        def repl(match: Any) -> str:
            original = match.group(0)
            placeholder = allocator.new(prefix)
            bindings.append({
                "tool_name": tool_name,
                "tool_original_index": tool_index,
                "path": path,
                "placeholder": placeholder,
                "value": original,
                "kind": kind,
            })
            return placeholder
        return pattern.sub(repl, text_in)

    # Priority: more specific first.
    text = _sub(text, patterns.file_uri,  "FILE_URI", "description_file_uri")
    text = _sub(text, patterns.win_path,  "PATH",     "description_win_path")
    text = _sub(text, patterns.unix_path, "PATH",     "description_unix_path")
    text = _sub(text, patterns.date,      "DATE",     "description_date")
    return text


def _normalize_defaults_lossless(
    obj: Any,
    *,
    patterns: Patterns,
    tool_name: str,
    tool_index: int | None,
    path: str,
    allocator: _PlaceholderAllocator,
    bindings: list[dict],
) -> None:
    """Walk a JSON-Schema tree and replace dynamic `default` strings in place."""
    if isinstance(obj, dict):
        if "default" in obj and isinstance(obj["default"], str):
            classified = _classify_default_string(obj["default"], patterns)
            if classified is not None:
                prefix, kind = classified
                placeholder = allocator.new(prefix)
                bindings.append({
                    "tool_name": tool_name,
                    "tool_original_index": tool_index,
                    "path": _json_pointer_join(path, "default"),
                    "placeholder": placeholder,
                    "value": obj["default"],
                    "kind": kind,
                })
                obj["default"] = placeholder

        for key, value in obj.items():
            _normalize_defaults_lossless(
                value,
                patterns=patterns,
                tool_name=tool_name, tool_index=tool_index,
                path=_json_pointer_join(path, key),
                allocator=allocator, bindings=bindings,
            )

    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _normalize_defaults_lossless(
                item,
                patterns=patterns,
                tool_name=tool_name, tool_index=tool_index,
                path=_json_pointer_join(path, str(idx)),
                allocator=allocator, bindings=bindings,
            )


def _normalize_descriptions_lossless(
    obj: Any,
    *,
    patterns: Patterns,
    tool_name: str,
    tool_index: int | None,
    path: str,
    allocator: _PlaceholderAllocator,
    bindings: list[dict],
) -> None:
    """Walk a dict/list tree and normalize every `description` string in place."""
    if isinstance(obj, dict):
        if "description" in obj and isinstance(obj["description"], str):
            obj["description"] = _normalize_text_with_bindings(
                obj["description"],
                patterns=patterns,
                tool_name=tool_name, tool_index=tool_index,
                path=_json_pointer_join(path, "description"),
                allocator=allocator, bindings=bindings,
            )
        for key, value in obj.items():
            _normalize_descriptions_lossless(
                value,
                patterns=patterns,
                tool_name=tool_name, tool_index=tool_index,
                path=_json_pointer_join(path, key),
                allocator=allocator, bindings=bindings,
            )
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _normalize_descriptions_lossless(
                item,
                patterns=patterns,
                tool_name=tool_name, tool_index=tool_index,
                path=_json_pointer_join(path, str(idx)),
                allocator=allocator, bindings=bindings,
            )


def normalize_tool_lossless(
    tool: dict,
    allocator: _PlaceholderAllocator | None = None,
    tool_index: int | None = None,
    *,
    patterns: Patterns = DEFAULT_PATTERNS,
) -> tuple[dict, list[dict]]:
    """Deep-copy `tool` with dynamic content replaced by unique placeholders.

    Returns (normalized_tool, bindings). The bindings record the original
    values, sufficient to reverse the normalization if needed.
    """
    tool_copy = _deep_copy_json(tool)
    bindings: list[dict] = []
    if allocator is None:
        allocator = _PlaceholderAllocator()

    tool_name = _get_tool_name(tool_copy)

    func = tool_copy.get("function")
    if isinstance(func, dict):
        if isinstance(func.get("description"), str):
            func["description"] = _normalize_text_with_bindings(
                func["description"],
                patterns=patterns,
                tool_name=tool_name, tool_index=tool_index,
                path="/function/description",
                allocator=allocator, bindings=bindings,
            )
        params = func.get("parameters")
        if isinstance(params, dict):
            _normalize_defaults_lossless(
                params, patterns=patterns,
                tool_name=tool_name, tool_index=tool_index,
                path="/function/parameters",
                allocator=allocator, bindings=bindings,
            )
            _normalize_descriptions_lossless(
                params, patterns=patterns,
                tool_name=tool_name, tool_index=tool_index,
                path="/function/parameters",
                allocator=allocator, bindings=bindings,
            )
    else:
        if isinstance(tool_copy.get("description"), str):
            tool_copy["description"] = _normalize_text_with_bindings(
                tool_copy["description"],
                patterns=patterns,
                tool_name=tool_name, tool_index=tool_index,
                path="/description",
                allocator=allocator, bindings=bindings,
            )

    return tool_copy, bindings


# ── Public variants (the 4 transforms) ──────────────────────────────────────

def sort_only(
    tools: list[dict], *, patterns: Patterns = DEFAULT_PATTERNS,
) -> list[dict]:
    """reorder=on, placeholder=off.

    Static tools (no dynamic content) first (alphabetized by name), then
    dynamic tools (alphabetized by name). Tool contents are NOT modified.
    """
    static: list[dict] = []
    dynamic: list[dict] = []
    for tool in tools:
        if has_dynamic_content(tool, patterns=patterns):
            dynamic.append(tool)
        else:
            static.append(tool)
    static.sort(key=_get_tool_name)
    dynamic.sort(key=_get_tool_name)
    return static + dynamic


def normalize_only(
    tools: list[dict], *, patterns: Patterns = DEFAULT_PATTERNS,
) -> list[dict]:
    """reorder=off, placeholder=on.

    Each tool is deep-copied with dynamic strings (paths / dates / file URIs)
    in `description` and `default` replaced by unique placeholders. Original
    tool order is preserved. Placeholder counter is shared across the batch,
    so each occurrence gets a unique name like `__PATH_0001__`.
    """
    allocator = _PlaceholderAllocator()
    out: list[dict] = []
    for idx, tool in enumerate(tools):
        normalized, _bindings = normalize_tool_lossless(
            tool, allocator=allocator, tool_index=idx, patterns=patterns,
        )
        out.append(normalized)
    return out


def sort_and_normalize(
    tools: list[dict], *, patterns: Patterns = DEFAULT_PATTERNS,
) -> list[dict]:
    """reorder=on, placeholder=on — equivalent to demo `sort_tools_for_caching_promax`.

    Normalize each tool, then partition by whether dynamic content remains
    (some tool descriptions may carry per-user content not covered by the
    regex set); static-after-normalization tools go first.
    """
    allocator = _PlaceholderAllocator()
    entries: list[tuple[dict, str]] = []
    for idx, tool in enumerate(tools):
        normalized, _bindings = normalize_tool_lossless(
            tool, allocator=allocator, tool_index=idx, patterns=patterns,
        )
        entries.append((normalized, _get_tool_name(normalized)))

    static: list[tuple[dict, str]] = []
    dynamic: list[tuple[dict, str]] = []
    for entry in entries:
        tool, _name = entry
        if has_dynamic_content(tool, patterns=patterns):
            dynamic.append(entry)
        else:
            static.append(entry)

    static.sort(key=lambda e: e[1])
    dynamic.sort(key=lambda e: e[1])
    return [t for t, _ in static] + [t for t, _ in dynamic]


def apply_variant(
    name: str, tools: list[dict], *, patterns: Patterns = DEFAULT_PATTERNS,
) -> list[dict]:
    """Dispatcher mapping a variant name to its transform."""
    if name == "base":
        return _deep_copy_json(tools)
    if name == "reorder":
        return sort_only(tools, patterns=patterns)
    if name == "placeholder":
        return normalize_only(tools, patterns=patterns)
    if name == "both":
        return sort_and_normalize(tools, patterns=patterns)
    raise ValueError(
        f"unknown variant {name!r}; expected one of {VARIANTS}"
    )


# ── Convenience: serialize-then-detect (for diagnostic tools) ───────────────

def serialize_tool(tool: dict) -> str:
    """Serialize a tool to its canonical JSON string (UTF-8 safe)."""
    return json.dumps(tool, ensure_ascii=False)
