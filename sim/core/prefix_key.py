"""Prefix-path key generation for offline KV-cache prefix-caching simulation.

Alignment with vLLM
-------------------
vLLM (vllm/v1/core/kv_cache_utils.py) computes block hashes as a chain:

    K[0] = hash_block_tokens(hash_fn, NONE_HASH, token_ids[0], extra_keys)
    K[i] = hash_block_tokens(hash_fn, K[i-1], token_ids[i], extra_keys)

where hash_block_tokens(hash_fn, parent, token_ids, extra_keys) serializes
the tuple (parent, token_ids_tuple, extra_keys) and applies sha256_cbor.
The parent of the first block is NONE_HASH (a random or env-seeded constant).

Offline trace adaptation
------------------------
The trace stores ``hash_ids[i]`` — a per-block content hash (SipHash-2-4 of
the block's token content). This is NOT the chained vLLM block hash; it is
the raw block content hash, analogous to ``curr_block_token_ids_tuple`` in
vLLM's hash_block_tokens.

Using ``hash_ids[i]`` directly as a cache key is INCORRECT:
two requests with different prefix paths but the same block content at
position i share a hash_ids[i] value, yet their KV tensors differ because
self-attention at position i includes all prior context. vLLM's chained hash
K[i] encodes the full prefix history and correctly distinguishes them.

This module reproduces vLLM's chained-hash semantics for offline traces:

    K[0] = sha256(NONE_SEED | encode(model_id) | encode(hash_ids[0]))
    K[i] = sha256(K[i-1]   | encode(model_id) | encode(hash_ids[i]))

where:
  - NONE_SEED is a fixed 32-byte constant (deterministic for offline
    reproducibility; vLLM uses a random or PYTHONHASHSEED-seeded NONE_HASH).
  - encode(s) = 4-byte big-endian length prefix + UTF-8 bytes (avoids
    concatenation ambiguity between fields).
  - model_id is included in every step because the offline simulator shares
    a single dict across all models; vLLM isolates models at BlockPool level
    (per-process separation), so model_id is NOT in vLLM's hash inputs.

Key type: bytes (32-byte SHA-256 digest). Python bytes are hashable and can
be used directly as dict keys. Call .hex() for human-readable display.
"""
from __future__ import annotations

import hashlib
from typing import List

# Fixed seed for the first block in any prefix chain.
# Analogous to vLLM's NONE_HASH, but deterministic (offline simulator must
# produce the same keys across independent runs without PYTHONHASHSEED).
_NONE_SEED: bytes = hashlib.sha256(b"two_queue_ttl_sim:none_hash:v1").digest()


def _encode_field(s: str) -> bytes:
    """Length-prefixed UTF-8 encoding to prevent field-boundary ambiguity.

    Without length prefixes, hash("ab", "c") == hash("a", "bc") because the
    raw concatenation is identical. Length prefixes remove this ambiguity.
    """
    b = s.encode("utf-8")
    return len(b).to_bytes(4, "big") + b


def _chain_step(parent: bytes, model_id: str, hash_id: str) -> bytes:
    """One chained-hash step, mirroring vLLM's hash_block_tokens.

    Corresponds to:
        hash_block_tokens(sha256_cbor, parent_block_hash, token_ids, extra_keys)
    where token_ids → hash_id and extra_keys → (model_id,).
    """
    h = hashlib.sha256()
    h.update(parent)
    h.update(_encode_field(model_id))
    h.update(_encode_field(hash_id))
    return h.digest()


def make_prefix_path_keys(model_id: str, hash_ids: List[str]) -> List[bytes]:
    """Generate chained prefix-path cache keys for a sequence of block hashes.

    Parameters
    ----------
    model_id:
        Model identifier string. Provides cross-model namespace isolation.
        (vLLM handles this at BlockPool level; offline simulator includes
        model_id in the key because all models share one cache dict.)
    hash_ids:
        Ordered per-block content hashes from the trace. hash_ids[i] is the
        content hash of the tokens in block i.

    Returns
    -------
    List[bytes]
        keys[i] is a 32-byte SHA-256 digest that uniquely identifies block i
        within the prefix path (model_id, hash_ids[0], ..., hash_ids[i]).
        Returns an empty list when hash_ids is empty.

    Correctness property
    --------------------
    For two independent calls, keys[i] == other_keys[i]  if and only if:
        model_id == other_model_id
        AND hash_ids[:i+1] == other_hash_ids[:i+1]

    This matches vLLM's prefix-cache hit condition: a block at position i
    can be reused only when the entire prefix chain [block_0 ... block_i]
    is identical — i.e. the same sequence of token content leading up to
    and including block i.
    """
    keys: List[bytes] = []
    parent = _NONE_SEED
    for hid in hash_ids:
        key = _chain_step(parent, model_id, hid)
        keys.append(key)
        parent = key
    return keys
