"""Tests for scripts/vendor_tokenizer_from_weights.py kv_meta derivation.

The risky part is computing kv_bytes_per_token from a model's config.json — a
wrong value silently corrupts the GB/min cache-pressure estimate. We pin the
math against the two already-vendored, hand-verified kv_meta.json values:
GLM-5 (MLA) = 89,856 and Qwen3-8B (GQA) = 147,456.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from vendor_tokenizer_from_weights import (  # noqa: E402
    _auto_map_modules,
    _resolve_text_config,
    compute_kv_bytes_per_token,
)


class TestMLA:
    def test_glm5_matches_vendored_value(self):
        # zai-org/GLM-5 config fields (MLA).
        config = {
            "num_hidden_layers": 78,
            "kv_lora_rank": 512,
            "qk_rope_head_dim": 64,
            "torch_dtype": "bfloat16",
        }
        meta = compute_kv_bytes_per_token(config)
        assert meta["kv_bytes_per_token"] == 89856
        assert meta["architecture"].startswith("MLA")
        assert "num_layers * (kv_lora_rank + qk_rope_head_dim)" in meta["formula"]

    def test_mla_takes_priority_over_gqa_fields(self):
        # A config with BOTH kv_lora_rank and GQA fields must use the MLA branch.
        config = {
            "num_hidden_layers": 4,
            "kv_lora_rank": 100,
            "qk_rope_head_dim": 28,
            "num_key_value_heads": 8,
            "head_dim": 128,
            "torch_dtype": "bfloat16",
        }
        meta = compute_kv_bytes_per_token(config)
        assert meta["kv_bytes_per_token"] == 4 * (100 + 28) * 2
        assert meta["architecture"].startswith("MLA")


class TestGQA:
    def test_qwen3_8b_matches_vendored_value(self):
        config = {
            "num_hidden_layers": 36,
            "num_key_value_heads": 8,
            "num_attention_heads": 32,
            "head_dim": 128,
            "torch_dtype": "bfloat16",
        }
        meta = compute_kv_bytes_per_token(config)
        assert meta["kv_bytes_per_token"] == 147456
        assert meta["architecture"] == "GQA"

    def test_head_dim_falls_back_to_hidden_over_heads(self):
        config = {
            "num_hidden_layers": 2,
            "num_key_value_heads": 4,
            "num_attention_heads": 16,
            "hidden_size": 2048,  # head_dim → 2048/16 = 128
            "torch_dtype": "float16",
        }
        meta = compute_kv_bytes_per_token(config)
        assert meta["head_dim"] == 128
        assert meta["kv_bytes_per_token"] == 2 * 2 * 4 * 128 * 2

    def test_mha_when_no_kv_heads(self):
        config = {
            "num_hidden_layers": 2,
            "num_attention_heads": 8,
            "head_dim": 64,
            "torch_dtype": "bfloat16",
        }
        meta = compute_kv_bytes_per_token(config)
        assert meta["architecture"] == "MHA"
        assert meta["num_kv_heads"] == 8


class TestDtype:
    def test_float32_is_four_bytes(self):
        config = {"num_hidden_layers": 1, "kv_lora_rank": 10, "qk_rope_head_dim": 6,
                  "torch_dtype": "float32"}
        meta = compute_kv_bytes_per_token(config)
        assert meta["dtype_bytes"] == 4

    def test_fp8_flags_a_note(self):
        config = {"num_hidden_layers": 1, "kv_lora_rank": 10, "qk_rope_head_dim": 6,
                  "torch_dtype": "float8_e4m3fn"}
        meta = compute_kv_bytes_per_token(config)
        assert meta["dtype_bytes"] == 1
        assert "dtype_note" in meta

    def test_unknown_dtype_defaults_to_two_with_note(self):
        config = {"num_hidden_layers": 1, "kv_lora_rank": 10, "qk_rope_head_dim": 6,
                  "torch_dtype": "weird"}
        meta = compute_kv_bytes_per_token(config)
        assert meta["dtype_bytes"] == 2
        assert "dtype_note" in meta


class TestResolveTextConfig:
    def test_nested_text_config_for_multimodal(self):
        # DeepSeek-OCR-style: LLM params nested under text_config.
        config = {
            "model_type": "deepseek_vl",
            "text_config": {
                "num_hidden_layers": 12,
                "kv_lora_rank": 512,
                "qk_rope_head_dim": 64,
                "torch_dtype": "bfloat16",
            },
        }
        meta = compute_kv_bytes_per_token(config)
        assert meta["num_layers"] == 12
        assert meta["kv_bytes_per_token"] == 12 * (512 + 64) * 2

    def test_raises_when_no_num_hidden_layers_anywhere(self):
        with pytest.raises(ValueError, match="num_hidden_layers"):
            _resolve_text_config({"model_type": "x", "foo": {"bar": 1}})


class TestAutoMap:
    def test_extracts_py_modules_from_auto_map(self, tmp_path):
        (tmp_path / "tokenizer_config.json").write_text(
            '{"auto_map": {"AutoTokenizer": ["tokenization_ds.Slow", "tokenization_ds_fast.Fast"]}}',
            encoding="utf-8",
        )
        (tmp_path / "tokenization_ds.py").write_text("# slow", encoding="utf-8")
        (tmp_path / "tokenization_ds_fast.py").write_text("# fast", encoding="utf-8")
        mods = _auto_map_modules(tmp_path)
        assert mods == ["tokenization_ds.py", "tokenization_ds_fast.py"]

    def test_no_auto_map_returns_empty(self, tmp_path):
        (tmp_path / "tokenizer_config.json").write_text('{"tokenizer_class": "X"}', encoding="utf-8")
        assert _auto_map_modules(tmp_path) == []

    def test_skips_referenced_module_not_on_disk(self, tmp_path):
        (tmp_path / "tokenizer_config.json").write_text(
            '{"auto_map": {"AutoTokenizer": ["tokenization_missing.Foo"]}}', encoding="utf-8"
        )
        assert _auto_map_modules(tmp_path) == []
