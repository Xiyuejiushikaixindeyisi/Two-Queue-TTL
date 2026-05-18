"""PromptEncoder protocol + byte-level / HF-token-level implementations.

Strategy Pattern (docs/step1_6_token_level_experiment_plan.md §3):

    raw_prompt: str
        ↓  encoder.encode()
    block_keys: list[bytes]  (32 bytes/key, SHA-256 prefix chain)
        ↓
    downstream: trie / LCP / chain forest / HTML  (unchanged)

Implementations:
- ByteLevelEncoder:  utf-8 bytes → 128-byte blocks → sha256 chain.
                     Matches existing scripts/verify_chain_path_closure.py
                     byte-by-byte (regression baseline).
- HFTokenEncoder:    model-agnostic. AutoTokenizer.from_pretrained(path) +
                     apply_chat_template → token_ids → 128-token blocks →
                     sha256 fallback chain. Works with any HF tokenizer
                     (GLM-5 / Qwen-V3 / Qwen-V3.5 / DeepSeek-V3 / …).
                     hash 算法不 bit-exact 复刻 vllm_hash (per plan §6),
                     deterministic 即可.
- GLM5TokenEncoder:  thin back-compat subclass of HFTokenEncoder. name 仍为
                     "glm5_token_v1" (历史 JSON metadata 兼容), default
                     tokenizer_path = "models/glm5_tokenizer".

All encoders are deterministic: same raw_prompt → same block_keys, and expose
a `block_unit` attribute ("bytes" | "tokens") for downstream banners/metadata.
"""
from __future__ import annotations

import hashlib
from typing import Protocol

from lib.hf_tokenizer import apply_template, load_kv_meta, load_tokenizer


class PromptEncoder(Protocol):
    """Common interface for byte-level / token-level prompt encoding."""

    name: str  # e.g. "byte_v1", "hf_token_v1", "glm5_token_v1" — JSON metadata + HTML
    block_unit: str  # "bytes" | "tokens" — drives downstream banner copy
    kv_bytes_per_token: int | None  # None for byte encoder; from kv_meta.json for HF

    def encode(self, raw_prompt: str) -> list[bytes]:
        """raw_prompt → ordered list of 32-byte SHA-256 prefix-chain keys."""
        ...


# ---------------------------------------------------------------------------
# Byte-level encoder (regression baseline, current production behaviour)
# ---------------------------------------------------------------------------


class ByteLevelEncoder:
    name = "byte_v1"
    block_unit = "bytes"
    hash_algo = "sha256_chain"
    kv_bytes_per_token: int | None = None  # byte 模式无 KV cache 估算

    def __init__(self, block_size_bytes: int = 128):
        self.block_size_bytes = block_size_bytes

    def encode(self, raw_prompt: str) -> list[bytes]:
        encoded = raw_prompt.encode("utf-8")
        keys: list[bytes] = []
        prev = b""
        for i in range(0, len(encoded), self.block_size_bytes):
            block = encoded[i:i + self.block_size_bytes]
            h = hashlib.sha256()
            h.update(prev)
            h.update(block)
            prev = h.digest()
            keys.append(prev)
        return keys


# ---------------------------------------------------------------------------
# HF token-level encoder (model-agnostic)
# ---------------------------------------------------------------------------


class HFTokenEncoder:
    """Token-level encoder backed by any HuggingFace tokenizer.

    `tokenizer_path` selects the model — local directory (推荐, 离线/Ascend) 或
    HF repo id. chat_mode 在 lib.hf_tokenizer.apply_template 实现, 模型无关.
    """

    name = "hf_token_v1"
    block_unit = "tokens"
    hash_algo = "sha256_chain_fallback"

    def __init__(
        self,
        tokenizer_path: str,
        chat_mode: str = "wrap_user",
        block_size_tokens: int = 128,
    ):
        self.tokenizer = load_tokenizer(tokenizer_path)
        self.chat_mode = chat_mode
        self.tokenizer_path = tokenizer_path
        self.block_size_tokens = block_size_tokens
        # KV cache 单 token 字节数, 用于 cache 压力 GB/min 估算
        self.kv_meta = load_kv_meta(tokenizer_path)
        self.kv_bytes_per_token = (
            self.kv_meta.get("kv_bytes_per_token") if self.kv_meta else None
        )

    def encode(self, raw_prompt: str) -> list[bytes]:
        token_ids = apply_template(self.tokenizer, raw_prompt, self.chat_mode)
        keys: list[bytes] = []
        prev = b""
        for i in range(0, len(token_ids), self.block_size_tokens):
            block = token_ids[i:i + self.block_size_tokens]
            # sha256 fallback: prev || ",".join(str(t) for t in block).encode("utf-8")
            # Hash algorithm need only be deterministic — see plan §6 decision.
            payload = ",".join(str(t) for t in block).encode("utf-8")
            h = hashlib.sha256()
            h.update(prev)
            h.update(payload)
            prev = h.digest()
            keys.append(prev)
        return keys


class GLM5TokenEncoder(HFTokenEncoder):
    """Back-compat alias: HFTokenEncoder pinned to models/glm5_tokenizer.

    Why kept: 已落盘的 user_summary.json metadata 中 `encoder_meta.name`
    字面值 "glm5_token_v1", 历史脚本默认 import GLM5TokenEncoder. 新代码
    应直接用 HFTokenEncoder; 此类仅作向后兼容.
    """

    name = "glm5_token_v1"

    def __init__(
        self,
        tokenizer_path: str = "models/glm5_tokenizer",
        chat_mode: str = "wrap_user",
        block_size_tokens: int = 128,
    ):
        super().__init__(tokenizer_path, chat_mode, block_size_tokens)


# ---------------------------------------------------------------------------
# Public helper: build encoder + emit JSON meta from argparse Namespace.
# Used by scripts/per_user_chain_analyzer.py and scripts/per_user_report_analyzer.py.
# ---------------------------------------------------------------------------


def build_encoder_from_args(args) -> tuple[PromptEncoder, dict]:
    """Read --encoder / --tokenizer-path / --chat-mode / --block-size from args.

    Encoder choices:
      - "byte":        ByteLevelEncoder (regression baseline)
      - "hf_token":    HFTokenEncoder (any HF tokenizer; --tokenizer-path 必填)
      - "glm5_token":  back-compat alias → GLM5TokenEncoder (name='glm5_token_v1',
                       default --tokenizer-path = models/glm5_tokenizer)

    Returns (encoder, encoder_meta_dict). encoder_meta is emitted to JSON for
    HTML banner + downstream consumers (see render_user_report_html.py).
    """
    if args.encoder == "byte":
        encoder: PromptEncoder = ByteLevelEncoder(block_size_bytes=args.block_size)
    elif args.encoder == "hf_token":
        encoder = HFTokenEncoder(
            tokenizer_path=args.tokenizer_path,
            chat_mode=args.chat_mode,
            block_size_tokens=args.block_size,
        )
    elif args.encoder == "glm5_token":
        encoder = GLM5TokenEncoder(
            tokenizer_path=args.tokenizer_path,
            chat_mode=args.chat_mode,
            block_size_tokens=args.block_size,
        )
    else:
        raise ValueError(f"unknown encoder: {args.encoder!r}")

    meta = {
        "name": encoder.name,
        "block_size": args.block_size,
        "block_unit": encoder.block_unit,
        "hash_algo": encoder.hash_algo,
        "chat_mode": getattr(encoder, "chat_mode", None),
        "tokenizer_path": getattr(encoder, "tokenizer_path", None),
        # KV cache 单 token 字节数 (None for byte mode / 缺 kv_meta.json)
        "kv_bytes_per_token": getattr(encoder, "kv_bytes_per_token", None),
    }
    return encoder, meta
