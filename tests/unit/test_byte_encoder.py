"""Unit tests for lib.prompt_encoder.ByteLevelEncoder.

Covers Step 1.6 plan §8.1 byte-level test plan:
- determinism
- prefix growth property (LCP necessary condition)
- regression: matches existing scripts/verify_chain_path_closure.py byte-by-byte
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

# Make scripts/ importable for the regression test against verify_chain_path_closure.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from lib.prompt_encoder import ByteLevelEncoder


# ---------------------------------------------------------------------------
# Basic determinism / shape
# ---------------------------------------------------------------------------


def test_empty_prompt_returns_empty_keys():
    enc = ByteLevelEncoder()
    assert enc.encode("") == []


def test_deterministic_same_input():
    enc = ByteLevelEncoder()
    assert enc.encode("hello world") == enc.encode("hello world")


def test_keys_are_32_byte_sha256_digests():
    enc = ByteLevelEncoder(block_size_bytes=4)
    keys = enc.encode("abcdefgh")  # 2 blocks
    assert len(keys) == 2
    for k in keys:
        assert isinstance(k, bytes)
        assert len(k) == 32


def test_block_count_scales_with_input_length():
    enc = ByteLevelEncoder(block_size_bytes=4)
    short = enc.encode("a" * 4)   # 1 block
    long_ = enc.encode("a" * 20)  # 5 blocks
    assert len(long_) == 5
    assert len(short) == 1


# ---------------------------------------------------------------------------
# Prefix growth (LCP necessary condition)
# ---------------------------------------------------------------------------


def test_prefix_growth_property():
    """Same prefix → identical leading keys (the basis of LCP / trie sharing)."""
    enc = ByteLevelEncoder(block_size_bytes=4)
    a = enc.encode("hello")        # 2 blocks: "hell", "o"
    b = enc.encode("hello world")  # 3 blocks: "hell", "o wo", "rld"
    # First 1 block is fully shared ("hell" identical → identical key)
    assert a[0] == b[0]


def test_one_byte_diff_breaks_chain_from_that_block():
    enc = ByteLevelEncoder(block_size_bytes=4)
    a = enc.encode("hellWORLD")  # "hell"+"WORL"+"D"
    b = enc.encode("hellWoRLD")  # "hell"+"WoRL"+"D"
    # block 0 same ("hell"), block 1 differs ("WORL" vs "WoRL")
    assert a[0] == b[0]
    assert a[1] != b[1]


def test_block_size_boundary():
    """A prompt exactly 1 block long encodes to exactly 1 key."""
    enc = ByteLevelEncoder(block_size_bytes=8)
    assert len(enc.encode("a" * 8)) == 1
    assert len(enc.encode("a" * 9)) == 2


# ---------------------------------------------------------------------------
# Regression: bit-exact match with existing scripts/verify_chain_path_closure.py
# ---------------------------------------------------------------------------


def test_matches_verify_chain_path_closure():
    """Output must be byte-by-byte identical to the existing reference impl.

    This is the production-baseline guarantee: all existing analyzers / HTML /
    quick_hit_rate.py must keep producing the same numbers after Step 1.6.
    """
    from verify_chain_path_closure import compute_prefix_path_keys, split_blocks

    for raw in [
        "hello",
        "中文测试 with mixed content 1234",
        "a" * 257,           # 3 blocks: 128/128/1
        "x" * 128,           # 1 block
        "x" * 128 + "y",     # 2 blocks
    ]:
        reference = compute_prefix_path_keys(split_blocks(raw, 128))
        ours = ByteLevelEncoder(block_size_bytes=128).encode(raw)
        assert ours == reference, f"mismatch on raw={raw[:30]!r}"


def test_matches_quick_hit_rate_logic():
    """Spot-check against the literal hash logic in scripts/quick_hit_rate.py:
    prev = sha256(prev || block).
    """
    enc = ByteLevelEncoder(block_size_bytes=128)
    raw = "ABC" * 100  # 300 bytes → 3 blocks
    ours = enc.encode(raw)

    # Manual computation
    data = raw.encode("utf-8")
    expected = []
    prev = b""
    for i in range(0, len(data), 128):
        block = data[i:i + 128]
        h = hashlib.sha256()
        h.update(prev)
        h.update(block)
        prev = h.digest()
        expected.append(prev)
    assert ours == expected


# ---------------------------------------------------------------------------
# Encoder protocol metadata
# ---------------------------------------------------------------------------


def test_name_metadata():
    """name attr is used in JSON metadata + HTML header — must be stable."""
    assert ByteLevelEncoder().name == "byte_v1"
