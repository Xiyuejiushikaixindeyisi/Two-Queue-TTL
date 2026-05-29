"""Tests for lib.prompt_encoder.resolve_max_input_tokens (over-length filter limit)."""
from __future__ import annotations

from lib.prompt_encoder import resolve_max_input_tokens


class _Tok:
    def __init__(self, mml):
        self.model_max_length = mml


class _Enc:
    def __init__(self, mml=None, has_tok=True):
        if has_tok:
            self.tokenizer = _Tok(mml)


def test_auto_from_tokenizer_model_max_length():
    assert resolve_max_input_tokens(_Enc(202752)) == 202752
    assert resolve_max_input_tokens(_Enc(131072)) == 131072


def test_explicit_overrides_auto():
    assert resolve_max_input_tokens(_Enc(202752), explicit=1000) == 1000


def test_disable_returns_none():
    assert resolve_max_input_tokens(_Enc(202752), disable=True) is None
    # disable wins even with explicit
    assert resolve_max_input_tokens(_Enc(202752), explicit=1000, disable=True) is None


def test_sentinel_model_max_length_means_no_limit():
    # HF "no limit" sentinel → too big → None
    assert resolve_max_input_tokens(_Enc(1000000000000000019884624838656)) is None


def test_byte_encoder_no_tokenizer_means_no_limit():
    assert resolve_max_input_tokens(_Enc(has_tok=False)) is None
