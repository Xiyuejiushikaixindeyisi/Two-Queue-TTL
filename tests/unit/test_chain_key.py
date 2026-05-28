"""Tests for lib/chain_key.py — must reproduce the legacy inline chain exactly."""
from __future__ import annotations

import hashlib

from lib.chain_key import sha256_chain_tokens


def _legacy_chain(token_ids, block_size):
    """The pre-centralization inline implementation (3 copies). Reference oracle."""
    out, prev = [], b""
    for i in range(0, len(token_ids), block_size):
        payload = ",".join(str(t) for t in token_ids[i:i + block_size]).encode("utf-8")
        h = hashlib.sha256()
        h.update(prev)
        h.update(payload)
        prev = h.digest()
        out.append(prev)
    return out


def test_matches_legacy_inline_exactly():
    for toks, bs in [([1, 2, 3, 4, 5], 2), (list(range(300)), 128), ([42], 128), ([], 8)]:
        assert sha256_chain_tokens(toks, bs) == _legacy_chain(toks, bs)


def test_full_32_byte_digests():
    keys = sha256_chain_tokens([1, 2, 3], 1)
    assert len(keys) == 3
    assert all(len(k) == 32 for k in keys)


def test_chain_is_prefix_sensitive():
    # same block content at position 1 but different position-0 prefix → different key
    a = sha256_chain_tokens([1, 2], 1)
    b = sha256_chain_tokens([9, 2], 1)
    assert a[0] != b[0]
    assert a[1] != b[1]          # position-1 key depends on the whole prefix


def test_convert_trace_hex_truncation_boundary():
    # convert_trace stores k.hex()[:16]; chain itself stays full-digest
    keys = sha256_chain_tokens([1, 2, 3, 4], 2)
    hex16 = [k.hex()[:16] for k in keys]
    assert all(len(h) == 16 for h in hex16)
