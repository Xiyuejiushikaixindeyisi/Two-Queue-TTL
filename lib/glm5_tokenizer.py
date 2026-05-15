"""GLM-5 tokenizer + chat-template wrapper for Step 1.6 token-level analysis.

P1 调研确认 (docs/step1_6_phase1_findings.md §6):
- tokenizer 来源:  zai-org/GLM-5 (公开仓库, 无 gating)
- 仅需 3 个文件: tokenizer.json / tokenizer_config.json / chat_template.jinja
- tokenizer_class=TokenizersBackend (非标准) → 必须 trust_remote_code=True
- GLM-5 是 thinking 模型, prefill 末尾自动追加 `<|assistant|><think>`
- wrap_user fixed overhead = 5 tokens
- 实测吞吐 ~720k tokens/s (BPE 线性)
- transformers 5.x `apply_chat_template(tokenize=True)` 返回 BatchEncoding
  → 必须两步走: tokenize=False 拿 string + tokenizer.encode() 拿 list[int]

3 个 chat_mode (与 plan §3.2 对齐):
- raw:        raw_prompt 已是 chat 字符串 (含 <|user|>/<|assistant|> 等) → 直接 encode
- wrap_user:  raw_prompt 是纯用户输入 → 包成 [{"role":"user","content":...}] 再 apply
- messages:   raw_prompt 是 JSON-encoded messages list → 直接 apply
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_VALID_MODES = ("raw", "wrap_user", "messages")
_DEFAULT_MODE = "wrap_user"


def load_tokenizer(model_path: str | Path) -> Any:
    """Load GLM-5 tokenizer from a local directory or HF repo id.

    Parameters
    ----------
    model_path:
        Either a local directory containing tokenizer.json/tokenizer_config.json/
        chat_template.jinja (e.g. "models/glm5_tokenizer/"), or an HF repo id
        (e.g. "zai-org/GLM-5"). Local paths are preferred for offline P4 runs.

    Returns
    -------
    A `transformers.PreTrainedTokenizer` instance.

    Raises
    ------
    ImportError if `transformers` is not installed in the current environment.
    """
    try:
        from transformers import AutoTokenizer
    except ImportError as e:
        raise ImportError(
            "GLM5TokenEncoder requires `transformers`. Install with: "
            "pip install transformers tokenizers jinja2"
        ) from e
    return AutoTokenizer.from_pretrained(str(model_path), trust_remote_code=True)


def apply_template(tokenizer: Any, raw_prompt: str, chat_mode: str) -> list[int]:
    """Convert one raw_prompt to GLM-5 token_ids under the chosen chat_mode.

    Two-step approach (transformers 5.x compat): render to string first, then
    encode to list[int]. `apply_chat_template(tokenize=True)` returns a
    BatchEncoding dict which is awkward to handle uniformly.

    Returns an empty list for empty input (caller filters these).
    """
    if not raw_prompt:
        return []
    if chat_mode not in _VALID_MODES:
        raise ValueError(f"unknown chat_mode {chat_mode!r}, must be one of {_VALID_MODES}")

    if chat_mode == "raw":
        return tokenizer.encode(raw_prompt, add_special_tokens=False)

    if chat_mode == "wrap_user":
        messages = [{"role": "user", "content": raw_prompt}]
    else:  # messages
        try:
            messages = json.loads(raw_prompt)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"chat_mode=messages requires JSON-encoded messages list, "
                f"got: {raw_prompt[:100]!r}"
            ) from e
        if not isinstance(messages, list):
            raise ValueError(
                f"chat_mode=messages: parsed JSON must be a list, got {type(messages).__name__}"
            )

    rendered = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    return tokenizer.encode(rendered, add_special_tokens=False)
