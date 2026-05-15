"""PromptEncoder protocol + byte-level / GLM-5 token-level implementations.

Step 1.6 Strategy Pattern (docs/step1_6_token_level_experiment_plan.md §3):

    raw_prompt: str
        ↓  encoder.encode()
    block_keys: list[bytes]  (32 bytes/key, SHA-256 prefix chain)
        ↓
    downstream: trie / LCP / chain forest / HTML  (unchanged)

Two implementations:
- ByteLevelEncoder:  utf-8 bytes → 128-byte blocks → sha256 chain.
                     Matches existing scripts/verify_chain_path_closure.py
                     byte-by-byte (regression baseline).
- GLM5TokenEncoder:  chat_template apply → token_ids → 128-token blocks →
                     sha256 fallback chain. hash 算法不 bit-exact 复刻 vllm_hash
                     (per plan §6 decision); hash deterministic 是充分条件.

Both encoders are deterministic: same raw_prompt → same block_keys.
"""
from __future__ import annotations

import hashlib
from typing import Protocol

from lib.glm5_tokenizer import apply_template, load_tokenizer


class PromptEncoder(Protocol):
    """Common interface for byte-level / token-level prompt encoding."""

    name: str  # e.g. "byte_v1", "glm5_token_v1" — appears in JSON metadata + HTML header

    def encode(self, raw_prompt: str) -> list[bytes]:
        """raw_prompt → ordered list of 32-byte SHA-256 prefix-chain keys."""
        ...


# ---------------------------------------------------------------------------
# Byte-level encoder (regression baseline, current production behaviour)
# ---------------------------------------------------------------------------


class ByteLevelEncoder:
    name = "byte_v1"

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
# GLM-5 token-level encoder
# ---------------------------------------------------------------------------


class GLM5TokenEncoder:
    name = "glm5_token_v1"

    def __init__(
        self,
        tokenizer_path: str = "models/glm5_tokenizer",
        chat_mode: str = "wrap_user",
        block_size_tokens: int = 128,
    ):
        self.tokenizer = load_tokenizer(tokenizer_path)
        self.chat_mode = chat_mode
        self.tokenizer_path = tokenizer_path
        self.block_size_tokens = block_size_tokens

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


# ---------------------------------------------------------------------------
# Public helper: build encoder + emit JSON meta from argparse Namespace.
# Used by scripts/per_user_chain_analyzer.py and scripts/per_user_report_analyzer.py.
# ---------------------------------------------------------------------------


def build_encoder_from_args(args) -> tuple[PromptEncoder, dict]:
    """Read --encoder / --tokenizer-path / --chat-mode / --block-size from args.

    Returns (encoder, encoder_meta_dict). encoder_meta is emitted to JSON for
    HTML banner + downstream consumers (see render_user_report_html.py).
    """
    if args.encoder == "byte":
        encoder = ByteLevelEncoder(block_size_bytes=args.block_size)
        meta = {
            "name": encoder.name,
            "block_size": args.block_size,
            "block_unit": "bytes",
            "hash_algo": "sha256_chain",
            "chat_mode": None,
            "tokenizer_path": None,
        }
    elif args.encoder == "glm5_token":
        encoder = GLM5TokenEncoder(
            tokenizer_path=args.tokenizer_path,
            chat_mode=args.chat_mode,
            block_size_tokens=args.block_size,
        )
        meta = {
            "name": encoder.name,
            "block_size": args.block_size,
            "block_unit": "tokens",
            "hash_algo": "sha256_chain_fallback",
            "chat_mode": args.chat_mode,
            "tokenizer_path": args.tokenizer_path,
        }
    else:
        raise ValueError(f"unknown encoder: {args.encoder!r}")
    return encoder, meta
