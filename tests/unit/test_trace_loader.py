"""Unit tests for trace_loader."""
import pytest

from sim.io.trace_loader import load_trace


SAMPLE_CSV = """\
timestamp,model_id,user_id,request_type,input_length,output_length,hash_ids
1001.0,qwen,u1,chat,256,50,h1|h2|h3
1000.0,qwen,u2,chat,128,30,h4|h5
"""

SAMPLE_JSONL = """\
{"timestamp": 2.0, "model_id": "qwen", "user_id": "u1", "request_type": "chat", "input_length": 256, "hash_ids": ["h1", "h2"]}
{"timestamp": 1.0, "model_id": "qwen", "user_id": "u2", "request_type": "agent", "input_length": 512, "hash_ids": ["h3", "h4", "h5"]}
"""


class TestCSVLoading:
    def test_loads_records(self, tmp_path):
        p = tmp_path / "trace.csv"
        p.write_text(SAMPLE_CSV)
        records = load_trace(str(p))
        assert len(records) == 2

    def test_sorted_by_timestamp(self, tmp_path):
        p = tmp_path / "trace.csv"
        p.write_text(SAMPLE_CSV)
        records = load_trace(str(p))
        assert records[0].timestamp == 1000.0
        assert records[1].timestamp == 1001.0

    def test_hash_ids_parsed(self, tmp_path):
        p = tmp_path / "trace.csv"
        p.write_text(SAMPLE_CSV)
        records = load_trace(str(p))
        r = next(r for r in records if r.user_id == "u1")
        assert r.hash_ids == ["h1", "h2", "h3"]

    def test_fields(self, tmp_path):
        p = tmp_path / "trace.csv"
        p.write_text(SAMPLE_CSV)
        records = load_trace(str(p))
        r = next(r for r in records if r.user_id == "u1")
        assert r.model_id == "qwen"
        assert r.input_length == 256
        assert r.output_length == 50


class TestJSONLLoading:
    def test_loads_records(self, tmp_path):
        p = tmp_path / "trace.jsonl"
        p.write_text(SAMPLE_JSONL)
        records = load_trace(str(p))
        assert len(records) == 2

    def test_sorted_by_timestamp(self, tmp_path):
        p = tmp_path / "trace.jsonl"
        p.write_text(SAMPLE_JSONL)
        records = load_trace(str(p))
        assert records[0].timestamp == 1.0
        assert records[1].timestamp == 2.0

    def test_hash_ids_as_list(self, tmp_path):
        p = tmp_path / "trace.jsonl"
        p.write_text(SAMPLE_JSONL)
        records = load_trace(str(p))
        assert records[0].hash_ids == ["h3", "h4", "h5"]


class TestErrorHandling:
    def test_unsupported_extension(self, tmp_path):
        p = tmp_path / "trace.txt"
        p.write_text("irrelevant")
        with pytest.raises(ValueError, match="Unsupported"):
            load_trace(str(p))
