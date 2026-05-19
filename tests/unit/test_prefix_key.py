"""Unit tests for sim.core.prefix_key.make_prefix_path_keys.

Verifies vLLM-aligned chained-hash semantics: block key at position i
must encode the full prefix path (model_id, hash_ids[0..i]), not just
hash_ids[i] alone.

Why this matters
----------------
The trace stores per-block content hashes (hash_ids[i]).  Two requests
with different prefix paths can share the same hash_ids[i] at position i
if they happen to have identical block content there.  A correct prefix-
cache simulation must NOT treat those as the same cached block, because
their KV tensors differ (self-attention at position i includes all prior
context).  make_prefix_path_keys encodes the full prefix chain in each
key, mirroring vLLM's hash_block_tokens chain.
"""

import hashlib


from sim.core.prefix_key import _NONE_SEED, _chain_step, make_prefix_path_keys


# ---------------------------------------------------------------------------
# Basic shape / type tests
# ---------------------------------------------------------------------------


def test_empty_hash_ids_returns_empty():
    assert make_prefix_path_keys("m1", []) == []


def test_single_block_returns_one_key():
    keys = make_prefix_path_keys("m1", ["h0"])
    assert len(keys) == 1


def test_key_type_is_bytes():
    keys = make_prefix_path_keys("m1", ["h0", "h1"])
    for k in keys:
        assert isinstance(k, bytes)


def test_key_length_is_32_bytes():
    """SHA-256 produces a 32-byte digest."""
    keys = make_prefix_path_keys("m1", ["h0", "h1", "h2"])
    for k in keys:
        assert len(k) == 32


def test_output_length_matches_hash_ids():
    hash_ids = ["a", "b", "c", "d", "e"]
    keys = make_prefix_path_keys("m1", hash_ids)
    assert len(keys) == len(hash_ids)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_input_produces_same_keys():
    """Keys must be deterministic across calls (no random seed)."""
    keys_a = make_prefix_path_keys("model_x", ["h0", "h1", "h2"])
    keys_b = make_prefix_path_keys("model_x", ["h0", "h1", "h2"])
    assert keys_a == keys_b


# ---------------------------------------------------------------------------
# Prefix-path isolation: changing any position breaks all later keys
# ---------------------------------------------------------------------------


def test_different_hash_ids_at_position_0_changes_all_keys():
    """Divergence at block 0 must propagate to all subsequent keys."""
    keys_a = make_prefix_path_keys("m1", ["X", "B", "C"])
    keys_b = make_prefix_path_keys("m1", ["Y", "B", "C"])
    # All three keys must differ because the chain is broken at position 0.
    assert keys_a[0] != keys_b[0], "position 0 key must differ"
    assert keys_a[1] != keys_b[1], "position 1 key must differ (parent changed)"
    assert keys_a[2] != keys_b[2], "position 2 key must differ (grandparent changed)"


def test_shared_prefix_gives_same_keys_divergence_breaks_later():
    """
    Requests that share a prefix must agree on the keys for the shared part,
    and differ only from the first divergence point onward.

    Request A: [h0, h1, h2, hA]
    Request B: [h0, h1, h2, hB]

    keys[0..2] must be equal; keys[3] must differ.
    """
    common = ["h0", "h1", "h2"]
    keys_a = make_prefix_path_keys("m1", common + ["hA"])
    keys_b = make_prefix_path_keys("m1", common + ["hB"])

    assert keys_a[0] == keys_b[0], "shared prefix block 0 must match"
    assert keys_a[1] == keys_b[1], "shared prefix block 1 must match"
    assert keys_a[2] == keys_b[2], "shared prefix block 2 must match"
    assert keys_a[3] != keys_b[3], "diverged block 3 must differ"


def test_same_hash_id_at_same_position_but_different_prefix_path():
    """
    This is the core semantic requirement: two requests that have the same
    block content at position i (same hash_ids[i]) but different prefix
    paths must produce different keys at position i.

    Simulates: request A = [X, Z], request B = [Y, Z].
    hash_ids[1] = "Z" for both, but prefix paths differ at position 0.
    In vLLM, K[1] differs because K[0] is the parent and K[0] differs.
    """
    keys_a = make_prefix_path_keys("m1", ["X", "Z"])
    keys_b = make_prefix_path_keys("m1", ["Y", "Z"])

    assert keys_a[1] != keys_b[1], (
        "Same hash_id at same position but different prefix paths must "
        "produce different keys (mirrors vLLM chained-hash behavior)"
    )


def test_prefix_of_longer_sequence_matches():
    """
    Short request [h0, h1] and long request [h0, h1, h2, h3] must share
    keys at positions 0 and 1 (same prefix path).
    """
    keys_short = make_prefix_path_keys("m1", ["h0", "h1"])
    keys_long = make_prefix_path_keys("m1", ["h0", "h1", "h2", "h3"])

    assert keys_short[0] == keys_long[0]
    assert keys_short[1] == keys_long[1]


# ---------------------------------------------------------------------------
# Model-id isolation
# ---------------------------------------------------------------------------


def test_different_model_ids_produce_different_keys():
    """
    Blocks with the same hash_ids but different model_ids must not collide.
    (vLLM handles this at BlockPool/process level; offline simulator includes
    model_id in the hash chain.)
    """
    keys_m1 = make_prefix_path_keys("model_A", ["h0", "h1"])
    keys_m2 = make_prefix_path_keys("model_B", ["h0", "h1"])

    assert keys_m1[0] != keys_m2[0]
    assert keys_m1[1] != keys_m2[1]


def test_same_model_id_different_hash_ids():
    """Basic sanity: different content → different keys even for same model."""
    keys_a = make_prefix_path_keys("m1", ["aaa"])
    keys_b = make_prefix_path_keys("m1", ["bbb"])
    assert keys_a[0] != keys_b[0]


# ---------------------------------------------------------------------------
# Chained structure: verify internal chain matches manual computation
# ---------------------------------------------------------------------------


def test_manual_chain_matches_function_output():
    """
    Verify that make_prefix_path_keys produces the exact same bytes as
    manually calling _chain_step in sequence.  This pins the implementation
    contract: anyone who needs to replicate the key outside this module can
    follow the same recipe.
    """
    model_id = "test_model"
    hash_ids = ["block0", "block1", "block2"]

    # Manual chain
    k0 = _chain_step(_NONE_SEED, model_id, "block0")
    k1 = _chain_step(k0, model_id, "block1")
    k2 = _chain_step(k1, model_id, "block2")
    expected = [k0, k1, k2]

    actual = make_prefix_path_keys(model_id, hash_ids)
    assert actual == expected


def test_none_seed_is_deterministic():
    """_NONE_SEED must not change across runs (no random element)."""
    expected_seed = hashlib.sha256(b"two_queue_ttl_sim:none_hash:v1").digest()
    assert _NONE_SEED == expected_seed


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_single_character_hash_ids():
    """Short hash_ids (even single chars) must not cause ambiguity."""
    keys = make_prefix_path_keys("m", ["a", "b", "a"])
    # Even though hash_ids[0] == hash_ids[2], their keys must differ
    # because the chain state differs.
    assert keys[0] != keys[2], (
        "Repeated hash_id at different positions must produce different keys"
    )


def test_empty_string_hash_id():
    """Empty string hash_id is valid and must produce a unique key."""
    keys_empty = make_prefix_path_keys("m1", [""])
    keys_nonempty = make_prefix_path_keys("m1", ["x"])
    assert len(keys_empty) == 1
    assert keys_empty[0] != keys_nonempty[0]


def test_empty_model_id():
    """Empty model_id is valid and must be distinct from non-empty."""
    keys_empty_model = make_prefix_path_keys("", ["h0"])
    keys_named_model = make_prefix_path_keys("m1", ["h0"])
    assert keys_empty_model[0] != keys_named_model[0]


def test_keys_are_all_distinct_for_unique_sequence():
    """All keys in a sequence with distinct hash_ids must themselves be distinct."""
    hash_ids = [f"block_{i}" for i in range(10)]
    keys = make_prefix_path_keys("m1", hash_ids)
    assert len(set(keys)) == len(keys), "all keys must be unique"


def test_hex_representation_is_64_chars():
    """SHA-256 hex digest is 64 characters; useful for human-readable display."""
    keys = make_prefix_path_keys("m1", ["h0"])
    assert len(keys[0].hex()) == 64
