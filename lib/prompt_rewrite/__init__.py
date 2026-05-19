"""Prompt rewrite analysis: tool reorder + placeholder replacement.

See docs/stage3_prompt_rewrite_plan.md for the design.

Public API (re-exported from submodules)
----------------------------------------
- Patterns / build_patterns / DEFAULT_PATTERNS    (lib.prompt_rewrite.patterns)
- detect_dynamic_labels(tool, patterns=...)       (lib.prompt_rewrite.detect)
- has_dynamic_content(tool, patterns=...)
- sort_only(tools, patterns=...)                  # reorder=on,  placeholder=off
- normalize_only(tools, patterns=...)             # reorder=off, placeholder=on
- sort_and_normalize(tools, patterns=...)         # reorder=on,  placeholder=on  (= demo promax)
"""
from lib.prompt_rewrite.patterns import (
    DEFAULT_PATTERNS,
    DEFAULT_UNIX_PATH_ROOTS,
    Patterns,
    build_patterns,
    load_patterns_config,
)
from lib.prompt_rewrite.detect import (
    apply_variant,
    detect_dynamic_labels,
    has_dynamic_content,
    normalize_only,
    normalize_tool_lossless,
    sort_and_normalize,
    sort_only,
)

__all__ = [
    "DEFAULT_PATTERNS",
    "DEFAULT_UNIX_PATH_ROOTS",
    "Patterns",
    "apply_variant",
    "build_patterns",
    "detect_dynamic_labels",
    "has_dynamic_content",
    "load_patterns_config",
    "normalize_only",
    "normalize_tool_lossless",
    "sort_and_normalize",
    "sort_only",
]
