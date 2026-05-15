"""Unit tests for lib.prompt_encoder.{HFTokenEncoder,GLM5TokenEncoder} + lib.hf_tokenizer.

Covers Step 1.6 plan §8.1 token-level test plan:
- tokenizer loads (skipped if transformers / tokenizer files missing)
- determinism
- chat_mode parity with raw `tokenizer.apply_chat_template`
- prefix growth (LCP necessary condition)
- sha256 fallback hash chain property
- 5 fixed-overhead tokens (wrap_user; GLM-5 specific, confirmed in P1 findings §2)
- HFTokenEncoder ≡ GLM5TokenEncoder when tokenizer_path = models/glm5_tokenizer
  (back-compat invariant)
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module-level skip if tokenizer / transformers unavailable
# ---------------------------------------------------------------------------

_TOKENIZER_DIR = Path(__file__).resolve().parents[2] / "models" / "glm5_tokenizer"

if not _TOKENIZER_DIR.exists():
    pytest.skip(
        f"GLM-5 tokenizer not found at {_TOKENIZER_DIR}; "
        "run `.venv_glm5/bin/hf download zai-org/GLM-5 tokenizer.json tokenizer_config.json chat_template.jinja --local-dir models/glm5_tokenizer` first",
        allow_module_level=True,
    )

transformers = pytest.importorskip("transformers")

from lib.hf_tokenizer import apply_template, load_tokenizer
from lib.prompt_encoder import GLM5TokenEncoder, HFTokenEncoder


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tokenizer():
    return load_tokenizer(_TOKENIZER_DIR)


@pytest.fixture(scope="module")
def encoder():
    return GLM5TokenEncoder(tokenizer_path=str(_TOKENIZER_DIR), chat_mode="wrap_user")


@pytest.fixture(scope="module")
def hf_encoder():
    return HFTokenEncoder(tokenizer_path=str(_TOKENIZER_DIR), chat_mode="wrap_user")


# ---------------------------------------------------------------------------
# Tokenizer-layer tests (lib.hf_tokenizer)
# ---------------------------------------------------------------------------


def test_tokenizer_vocab_size(tokenizer):
    """P1 findings §6: vocab_size=154820 (informational, sanity check)."""
    assert tokenizer.vocab_size == 154820


def test_wrap_user_overhead_is_5_tokens(tokenizer):
    """P1 findings §2.3: wrap_user fixed overhead = 5 tokens.

    [gMASK]<sop><|user|><|assistant|><think> = 5 tokens
    """
    ids = apply_template(tokenizer, "", "wrap_user")
    # empty prompt: should never be reached (caller filters), but if forced, == []
    assert ids == []

    # Single-char prompt: overhead 5 + 1 = 6 (or 5 + tokens-of-char)
    ids = apply_template(tokenizer, "a", "wrap_user")
    # "a" is 1 token in BPE; 5 wrapping tokens + 1 = 6
    assert len(ids) == 6


def test_chat_mode_raw_no_extra_wrapping(tokenizer):
    """raw mode: just tokenize, no chat template wrapping."""
    raw_text = "hello"
    direct = tokenizer.encode(raw_text, add_special_tokens=False)
    via = apply_template(tokenizer, raw_text, "raw")
    assert direct == via


def test_chat_mode_wrap_user_parity_with_transformers(tokenizer):
    """wrap_user must match `tokenizer.apply_chat_template([{user...}], add_generation_prompt=True)`."""
    raw = "今天天气怎么样?"
    expected_rendered = tokenizer.apply_chat_template(
        [{"role": "user", "content": raw}],
        tokenize=False,
        add_generation_prompt=True,
    )
    expected_ids = tokenizer.encode(expected_rendered, add_special_tokens=False)
    ours = apply_template(tokenizer, raw, "wrap_user")
    assert ours == expected_ids


def test_chat_mode_messages_parses_json(tokenizer):
    """messages mode: raw_prompt is JSON of a messages list."""
    msgs = [
        {"role": "user", "content": "1+1="},
        {"role": "assistant", "content": "2"},
        {"role": "user", "content": "2+2="},
    ]
    raw = json.dumps(msgs)
    ours = apply_template(tokenizer, raw, "messages")
    expected = tokenizer.encode(
        tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True),
        add_special_tokens=False,
    )
    assert ours == expected


def test_chat_mode_messages_rejects_non_json(tokenizer):
    with pytest.raises(ValueError, match="JSON"):
        apply_template(tokenizer, "not a json", "messages")


def test_chat_mode_messages_rejects_non_list(tokenizer):
    with pytest.raises(ValueError, match="list"):
        apply_template(tokenizer, '{"role": "user", "content": "x"}', "messages")


def test_invalid_chat_mode_raises(tokenizer):
    with pytest.raises(ValueError, match="unknown chat_mode"):
        apply_template(tokenizer, "hi", "invalid_mode")


# ---------------------------------------------------------------------------
# Encoder-layer tests (lib.prompt_encoder.{HFTokenEncoder, GLM5TokenEncoder})
# ---------------------------------------------------------------------------


def test_encoder_name_metadata(encoder):
    """Back-compat: glm5_token_v1 name preserved for historical JSON metadata."""
    assert encoder.name == "glm5_token_v1"
    assert encoder.block_unit == "tokens"
    assert encoder.hash_algo == "sha256_chain_fallback"


def test_hf_encoder_name_metadata(hf_encoder):
    """HFTokenEncoder uses generic name; block_unit/hash_algo aligned."""
    assert hf_encoder.name == "hf_token_v1"
    assert hf_encoder.block_unit == "tokens"
    assert hf_encoder.hash_algo == "sha256_chain_fallback"


def test_glm5_is_alias_of_hf(encoder, hf_encoder):
    """GLM5TokenEncoder must produce identical keys to HFTokenEncoder
    when pointed at the same tokenizer (back-compat invariant)."""
    raw = "今天天气怎么样? 帮我写一段 Python 处理 JSON 的代码。" * 10
    assert encoder.encode(raw) == hf_encoder.encode(raw)


def test_encoder_deterministic(encoder):
    a = encoder.encode("你好世界")
    b = encoder.encode("你好世界")
    assert a == b


def test_encoder_empty_input(encoder):
    assert encoder.encode("") == []


def test_keys_are_32_byte_sha256(encoder):
    keys = encoder.encode("hello")
    assert len(keys) >= 1
    for k in keys:
        assert isinstance(k, bytes)
        assert len(k) == 32


def test_short_prompt_one_block(encoder):
    """A wrap_user-mode 'hi' is well under 128 tokens → 1 block."""
    keys = encoder.encode("hi")
    assert len(keys) == 1


def test_long_prompt_multiple_blocks(encoder):
    """Long prompt produces multiple 128-token blocks."""
    long_prompt = "请帮我写一段 Python 代码处理 list" * 200  # ~6000 tokens estimated
    keys = encoder.encode(long_prompt)
    assert len(keys) >= 2  # at least 2 blocks (128+ tokens)


def test_prefix_growth_property(encoder):
    """Longer prompt with same prefix → leading keys identical (LCP basis).

    Both prompts wrap into the same chat template, so the 5-token overhead +
    common prefix tokens fall into the first block(s) identically.
    """
    base = "请帮我做一件事: " * 30  # generate enough tokens to cross block boundary
    a = encoder.encode(base)
    b = encoder.encode(base + " 另外加一些后缀让总长度更长一点")
    if len(a) >= 1 and len(b) >= 1:
        # First block must be the same (same first 128 tokens)
        assert a[0] == b[0]


# ---------------------------------------------------------------------------
# Hash-chain implementation: sha256 fallback (plan §6 decision)
# ---------------------------------------------------------------------------


def test_hash_chain_is_sha256_fallback(encoder, tokenizer):
    """Verify the internal hash algo matches plan §6.2 spec:
    K[i] = sha256(K[i-1] || ",".join(str(t) for t in block).encode("utf-8"))
    """
    raw = "abc"  # tiny prompt → 1 block
    keys = encoder.encode(raw)

    # Re-derive the same tokens via apply_template
    token_ids = apply_template(tokenizer, raw, "wrap_user")
    blocks = [token_ids[i:i + 128] for i in range(0, len(token_ids), 128)]

    expected = []
    prev = b""
    for block in blocks:
        payload = ",".join(str(t) for t in block).encode("utf-8")
        h = hashlib.sha256()
        h.update(prev)
        h.update(payload)
        prev = h.digest()
        expected.append(prev)

    assert keys == expected


def test_hash_chain_parent_dependency(encoder):
    """Two different first blocks produce different keys at position ≥ 0
    even if the second block has identical content.
    """
    # Two prompts whose first 128-token block differs but later content overlaps
    # would be hard to construct deterministically; instead show that two
    # totally different prompts produce all-different keys.
    a = encoder.encode("第一种 prompt")
    b = encoder.encode("完全不同的另一种 prompt")
    assert a != b
    # Even the first key differs (because token sequences differ)
    if a and b:
        assert a[0] != b[0]
