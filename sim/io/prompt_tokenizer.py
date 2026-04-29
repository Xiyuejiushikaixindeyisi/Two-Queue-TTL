"""Convert raw_prompt text to per-block content hash IDs.

Pipeline
--------
raw_prompt (str)
    ↓  tokenize()
token_ids (List[int])
    ↓  chunk into blocks of block_size
blocks (List[List[int]])
    ↓  sha256_block_hash()  [deterministic, no external dependency]
hash_ids (List[str])          ← same format as TraceRecord.hash_ids

Tokenizer backend (auto-selected at import time, highest priority first)
------------------------------------------------------------------------
1. tiktoken cl100k_base  — if `pip install tiktoken` is available.
   Covers GPT-4 / Qwen-series tokenizer (BPE vocabulary ~100k).
2. UTF-8 byte-level       — stdlib-only fallback.
   Each byte is treated as one "token" (token_id = byte value 0-255).
   Produces larger block counts for the same text but remains internally
   consistent: same prompt → same hash_ids regardless of run.

Hash function
-------------
SHA-256 of the block's token IDs packed as little-endian int32 array,
truncated to 16 hex characters (64-bit).  Properties for offline simulation:
  - Deterministic (no PYTHONHASHSEED dependence)
  - Collision probability < 1/2^64 per block pair
  - Same token content → identical hash (required for cache hit detection)

This is functionally equivalent to SipHash-2-4 for simulation purposes.
SipHash is used in vLLM runtime for speed (C-level, 64-bit output); here
we prefer stdlib correctness over speed optimisation.
"""
from __future__ import annotations

import hashlib
import struct
from typing import List


# ---------------------------------------------------------------------------
# Tokenizer backend selection
# ---------------------------------------------------------------------------

def _try_tiktoken():
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        def tokenize(text: str) -> List[int]:
            return enc.encode(text)
        return tokenize, "tiktoken:cl100k_base"
    except ImportError:
        return None, None


def _bytes_tokenize(text: str) -> List[int]:
    """Fallback: each UTF-8 byte is one 'token'."""
    return list(text.encode("utf-8"))


_tokenize_fn, _BACKEND = _try_tiktoken()
if _tokenize_fn is None:
    _tokenize_fn = _bytes_tokenize
    _BACKEND = "utf8_bytes"


def tokenizer_backend() -> str:
    """Return the name of the active tokenizer backend."""
    return _BACKEND


# ---------------------------------------------------------------------------
# Block hash
# ---------------------------------------------------------------------------

def _block_hash(token_ids: List[int]) -> str:
    """Deterministic hash of one block's token IDs → 16-char hex string."""
    data = struct.pack(f"<{len(token_ids)}i", *token_ids)
    return hashlib.sha256(data).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_hash_ids(text: str, block_size: int = 128) -> List[str]:
    """Tokenize text and return per-block content hashes.

    Parameters
    ----------
    text:
        Raw prompt text (any length, including 128k-token inputs).
    block_size:
        Number of tokens per cache block.  Must match the block_size
        used in SimConfig so that hit/miss counts are meaningful.

    Returns
    -------
    List of hex strings, one per block.  An empty prompt returns ['<empty>'].
    """
    token_ids = _tokenize_fn(text)
    if not token_ids:
        return ["<empty>"]

    hashes: List[str] = []
    for start in range(0, len(token_ids), block_size):
        block = token_ids[start: start + block_size]
        hashes.append(_block_hash(block))
    return hashes


def token_count(text: str) -> int:
    """Return the token count for text under the active backend."""
    return len(_tokenize_fn(text))
