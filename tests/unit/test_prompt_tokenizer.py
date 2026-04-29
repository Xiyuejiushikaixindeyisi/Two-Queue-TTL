"""Unit tests for prompt_tokenizer and raw_trace_loader."""
import csv
import os
import tempfile

import pytest

from sim.io.prompt_tokenizer import compute_hash_ids, token_count, tokenizer_backend


class TestTokenizerBackend:
    def test_backend_name_is_known(self):
        assert tokenizer_backend() in ("tiktoken:cl100k_base", "utf8_bytes")

    def test_empty_string_returns_empty_sentinel(self):
        assert compute_hash_ids("") == ["<empty>"]

    def test_short_text_single_block(self):
        result = compute_hash_ids("hello", block_size=128)
        assert len(result) == 1
        assert len(result[0]) == 16   # 16 hex chars

    def test_deterministic_same_text(self):
        a = compute_hash_ids("the quick brown fox", block_size=128)
        b = compute_hash_ids("the quick brown fox", block_size=128)
        assert a == b

    def test_different_text_different_hash(self):
        a = compute_hash_ids("hello world", block_size=128)
        b = compute_hash_ids("hello worlds", block_size=128)
        assert a != b

    def test_block_count_scales_with_length(self):
        short = compute_hash_ids("a" * 10, block_size=4)
        long  = compute_hash_ids("a" * 40, block_size=4)
        assert len(long) > len(short)

    def test_prefix_sharing(self):
        """Two requests with a common prefix share the same leading hash_ids."""
        prefix = "system: you are a helpful assistant. "
        a = compute_hash_ids(prefix + "user: foo", block_size=8)
        b = compute_hash_ids(prefix + "user: bar", block_size=8)
        # The first block comes from the shared prefix — hashes must match
        assert a[0] == b[0]

    def test_hash_length_is_16(self):
        for text in ["", "x", "x" * 1000]:
            result = compute_hash_ids(text, block_size=32)
            for h in result:
                if h != "<empty>":
                    assert len(h) == 16

    def test_block_size_1_many_blocks(self):
        text = "abc"   # at least 3 UTF-8 bytes / tokens
        result = compute_hash_ids(text, block_size=1)
        assert len(result) >= 3

    def test_token_count_positive(self):
        assert token_count("hello world") > 0

    def test_long_prompt(self):
        # Simulate a ~1k-word prompt; should not raise
        long_text = ("The assistant helps users with tasks. " * 200)
        result = compute_hash_ids(long_text, block_size=128)
        assert len(result) >= 1


class TestRawTraceLoader:
    def _make_csv(self, rows: list[dict], path: str) -> None:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["request_id", "user_id", "raw_prompt", "timestamp"])
            writer.writeheader()
            writer.writerows(rows)

    def test_basic_load(self):
        from sim.io.raw_trace_loader import load_raw_trace
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w",
                                         delete=False, newline="") as f:
            path = f.name
        try:
            self._make_csv([
                {"request_id": "r1", "user_id": "u1",
                 "raw_prompt": "hello world", "timestamp": "1.0"},
                {"request_id": "r2", "user_id": "u2",
                 "raw_prompt": "foo bar baz", "timestamp": "2.0"},
            ], path)
            trace = load_raw_trace(path, block_size=128, verbose=False)
            assert len(trace) == 2
            assert trace[0].timestamp == 1.0
            assert trace[0].user_id == "u1"
            assert trace[0].model_id == "default"
            assert len(trace[0].hash_ids) >= 1
        finally:
            os.unlink(path)

    def test_sorted_by_timestamp(self):
        from sim.io.raw_trace_loader import load_raw_trace
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w",
                                         delete=False, newline="") as f:
            path = f.name
        try:
            self._make_csv([
                {"request_id": "r2", "user_id": "u1",
                 "raw_prompt": "second", "timestamp": "2.0"},
                {"request_id": "r1", "user_id": "u1",
                 "raw_prompt": "first", "timestamp": "1.0"},
            ], path)
            trace = load_raw_trace(path, verbose=False)
            assert trace[0].timestamp < trace[1].timestamp
        finally:
            os.unlink(path)

    def test_missing_column_raises(self):
        from sim.io.raw_trace_loader import load_raw_trace
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w",
                                         delete=False, newline="") as f:
            path = f.name
            f.write("request_id,user_id,timestamp\nr1,u1,1.0\n")
        try:
            with pytest.raises(ValueError, match="raw_prompt"):
                load_raw_trace(path, verbose=False)
        finally:
            os.unlink(path)

    def test_prefix_sharing_in_trace(self):
        """Two requests with same prefix → first hash_id matches."""
        from sim.io.raw_trace_loader import load_raw_trace
        shared = "System: you are helpful. " * 20
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w",
                                         delete=False, newline="") as f:
            path = f.name
        try:
            self._make_csv([
                {"request_id": "r1", "user_id": "u1",
                 "raw_prompt": shared + "User: what is 2+2?", "timestamp": "1.0"},
                {"request_id": "r2", "user_id": "u2",
                 "raw_prompt": shared + "User: tell me a joke.", "timestamp": "2.0"},
            ], path)
            trace = load_raw_trace(path, block_size=8, verbose=False)
            # First block of both records covers the shared prefix → same hash
            assert trace[0].hash_ids[0] == trace[1].hash_ids[0]
        finally:
            os.unlink(path)
