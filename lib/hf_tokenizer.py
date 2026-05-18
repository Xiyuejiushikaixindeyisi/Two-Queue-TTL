"""HuggingFace tokenizer + chat-template wrapper for token-level analysis.

通用 HF tokenizer 加载器, 不与具体模型耦合. 任何 HF AutoTokenizer 兼容的
tokenizer (GLM-5 / Qwen-V3 / Qwen-V3.5 / DeepSeek-V3 / …) 都可以通过本模块
加载, 配合 lib.prompt_encoder.HFTokenEncoder 完成 token-level 编码.

加载方式 (load_tokenizer):
- AutoTokenizer.from_pretrained(path, trust_remote_code=True)
- path 可以是本地目录 (推荐, 离线/Ascend 场景) 或 HF repo id

3 个 chat_mode:
- raw:        raw_prompt 已是 chat 字符串 (含模型对应 <|user|>/<|assistant|> 等
              special tokens) → 直接 encode (add_special_tokens=False)
- wrap_user:  raw_prompt 是纯用户输入 → 包成 [{"role":"user","content":...}]
              再走 apply_chat_template(add_generation_prompt=True)
- messages:   raw_prompt 是 JSON-encoded messages list → 直接 apply_chat_template

实现细节:
- transformers 5.x `apply_chat_template(tokenize=True)` 返回 BatchEncoding,
  不便统一处理 → 两步走: tokenize=False 拿 rendered string, 再 encode 拿 list[int]
- 不同模型的 wrap_user 固定开销 token 数不同 (GLM-5: 5 = [gMASK]<sop><|user|>
  <|assistant|><think>; Qwen3 没 <think> → 4; DeepSeek 不同), 但 chat_template
  已封装这些差异, 算法层只看最终 token 序列做 LCP, 不依赖具体 overhead 值.

历史调研: docs/step1_6_phase1_findings.md §6 (GLM-5 调研, BPE 吞吐 ~720k tokens/s).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_VALID_MODES = ("raw", "wrap_user", "messages")
_DEFAULT_MODE = "wrap_user"


def load_kv_meta(tokenizer_path: str | Path) -> dict | None:
    """读 models/<name>_tokenizer/kv_meta.json (若存在).

    用于 cache 压力 GB/min 估算 (kv_bytes_per_token 字段). 缺失时返回 None,
    pipeline 仍能跑, 只是不输出 GB 列.

    JSON schema (必须字段): kv_bytes_per_token (int). 其它 (model / formula /
    num_layers / dtype) 是 audit 信息, 不影响计算.
    """
    meta_path = Path(tokenizer_path) / "kv_meta.json"
    if not meta_path.is_file():
        return None
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    if "kv_bytes_per_token" not in meta:
        raise ValueError(
            f"{meta_path}: missing required field 'kv_bytes_per_token'"
        )
    return meta


def load_tokenizer(model_path: str | Path) -> Any:
    """Load an HF tokenizer from a local directory or HF repo id.

    Parameters
    ----------
    model_path:
        Either a local directory containing tokenizer.json/tokenizer_config.json/
        chat_template.jinja (e.g. "models/glm5_tokenizer/", "models/qwen_v3_tokenizer/"),
        or an HF repo id (e.g. "zai-org/GLM-5"). Local paths are preferred for
        offline / air-gapped runs (Ascend, etc.).

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
            "HFTokenEncoder requires `transformers`. Install with: "
            "pip install transformers tokenizers jinja2"
        ) from e
    return AutoTokenizer.from_pretrained(str(model_path), trust_remote_code=True)


def apply_template(tokenizer: Any, raw_prompt: str, chat_mode: str) -> list[int]:
    """Convert one raw_prompt to token_ids under the chosen chat_mode (model-agnostic).

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
