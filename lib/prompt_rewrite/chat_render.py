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

Production-data fix-up (2026-05-19): assistant messages in real traces have
`tool_calls[i].function.arguments` as a JSON-encoded **string** (OpenAI's
chat-completions wire format), but GLM-5's template iterates the field with
`.items()`, expecting a **dict**. We parse the string back to a dict before
rendering — see `_normalize_messages`.
"""
from __future__ import annotations

import json
from typing import Any


def _normalize_messages(messages: list[dict]) -> list[dict]:
    """Patch up production-data quirks before handing messages to the template.

    Currently handles one issue:
    - `tool_calls[i].function.arguments` may be a JSON-encoded string (OpenAI
      wire format) but the GLM-5 template iterates it with `.items()`. We
      parse the string back to a dict when possible.

    Returns a new messages list with shallow copies; original input is not
    mutated.
    """
    out: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        if not m.get("tool_calls"):
            out.append(m)
            continue
        m2 = dict(m)
        new_tcs: list[dict] = []
        for tc in m2["tool_calls"] or []:
            if not isinstance(tc, dict):
                new_tcs.append(tc)
                continue
            tc2 = dict(tc)
            func = tc2.get("function")
            if isinstance(func, dict):
                args = func.get("arguments")
                if isinstance(args, str):
                    try:
                        parsed = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        # Leave as-is; render will raise downstream and the
                        # row will be skipped by the caller.
                        parsed = None
                    if isinstance(parsed, dict):
                        func2 = dict(func)
                        func2["arguments"] = parsed
                        tc2["function"] = func2
            new_tcs.append(tc2)
        m2["tool_calls"] = new_tcs
        out.append(m2)
    return out


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
        _normalize_messages(messages),
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
        _normalize_messages(messages),
        tools=tools if tools else None,
        tokenize=False,
        add_generation_prompt=add_generation_prompt,
    )
