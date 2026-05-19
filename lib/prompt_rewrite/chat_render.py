"""Offline chat-template rendering with `tools` support.

Wraps `tokenizer.apply_chat_template(messages, tools=...)` and returns
`token_ids: list[int]`, mirroring the two-step (render → encode) approach
in `lib/hf_tokenizer.py::apply_template`.

Why this is a separate module: `lib.hf_tokenizer.apply_template` is for
single `raw_prompt` strings (raw / wrap_user / messages modes). The chat-trace
pipeline needs structured `(messages, tools)` input — different signature,
different rendering path. Keeping them apart avoids overloading one function
with two unrelated argument shapes.

GLM-5's `chat_template.jinja` natively handles `tools` (wraps them in a
`<tools>` XML block inside a system message). If `tools` is empty or None,
the system block is omitted and the rendering reduces to standard chat.
"""
from __future__ import annotations

from typing import Any


def render_to_tokens(
    tokenizer: Any,
    messages: list[dict],
    tools: list[dict] | None,
    add_generation_prompt: bool = True,
) -> list[int]:
    """Apply chat template (with tools) and return token ids.

    Parameters
    ----------
    tokenizer:
        An HF tokenizer with `apply_chat_template` (returned by
        `lib.hf_tokenizer.load_tokenizer`).
    messages:
        Standard chat messages list, e.g. `[{"role": "user", "content": "..."}]`.
    tools:
        Tool definitions (OpenAI / GLM-5 schema), or None / empty for a
        chat-without-tools render.
    add_generation_prompt:
        Pass through to `apply_chat_template`. Default True (matches what an
        inference server would render at request time).
    """
    rendered = tokenizer.apply_chat_template(
        messages,
        tools=tools if tools else None,
        tokenize=False,
        add_generation_prompt=add_generation_prompt,
    )
    return tokenizer.encode(rendered, add_special_tokens=False)


def render_to_text(
    tokenizer: Any,
    messages: list[dict],
    tools: list[dict] | None,
    add_generation_prompt: bool = True,
) -> str:
    """Apply chat template and return the rendered string (for debugging / diffs)."""
    return tokenizer.apply_chat_template(
        messages,
        tools=tools if tools else None,
        tokenize=False,
        add_generation_prompt=add_generation_prompt,
    )
