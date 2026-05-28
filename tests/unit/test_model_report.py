"""Tests for scripts/model_report.py (single CSV → model-level HTML report).

Fake encoder mimics the chain-hash property (key at position i encodes the whole
prefix) and implements encode_with_length so we can verify, without transformers:
- model-level pooled hit rate (cross-app reuse counted),
- per-app ISOLATED hit rate + LCP lists (items 2 & 4 are the same computation),
- reuse_time = inter-access gap, computed in chronological order,
- avg length in tokens, request-share pct, time span.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from model_report import analyze_model_csv, build_html, fmt_duration  # noqa: E402


class FakeEncoder:
    block_unit = "tokens"

    def encode_with_length(self, prompt: str):
        keys, acc = [], ""
        words = prompt.split()
        for w in words:
            acc += w + "|"
            keys.append(acc.encode())
        return keys, len(words)

    def encode(self, prompt: str):
        return self.encode_with_length(prompt)[0]


HEADER = ["request_id", "user_id", "raw_prompt", "timestamp"]


def _csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_model_level_pooled_hit_rate(tmp_path):
    p = tmp_path / "d.csv"
    _csv(p, [
        {"request_id": "1", "user_id": "a", "raw_prompt": "x y z", "timestamp": "0"},
        {"request_id": "2", "user_id": "a", "raw_prompt": "x y z", "timestamp": "60"},
    ])
    m = analyze_model_csv(p, FakeEncoder())["model"]
    assert m["total_blocks"] == 6
    assert m["unique_blocks"] == 3
    assert m["ideal_hit_rate"] == 0.5
    assert m["reqs"] == 2
    assert m["avg_len"] == 3.0           # tokens: (3+3)/2
    assert m["span_seconds"] == 60
    assert m["n_apps"] == 1


def test_app_hit_rate_is_isolated_not_pooled(tmp_path):
    # app1 establishes "x"; app2's "x z" must NOT count "x" as a hit at app level,
    # even though the model-pooled run would.
    p = tmp_path / "d.csv"
    _csv(p, [
        {"request_id": "1", "user_id": "app1", "raw_prompt": "x y", "timestamp": "0"},
        {"request_id": "2", "user_id": "app2", "raw_prompt": "x z", "timestamp": "10"},
    ])
    stats = analyze_model_csv(p, FakeEncoder())
    apps = {r["app_id"]: r for r in stats["apps"]}
    assert apps["app2"]["ideal_hit_rate"] == 0.0      # isolated: x is new to app2
    # but the model pooled run counts the shared "x|"
    assert stats["model"]["ideal_hit_rate"] == 1 / 4  # total 4, unique 3 → hit 1


def test_app_lcp_lists_match_app_hit_rate(tmp_path):
    # app hit_rate == sum(LCP)/sum(blocks) — items 2 and 4 are one computation.
    p = tmp_path / "d.csv"
    _csv(p, [
        {"request_id": "1", "user_id": "a", "raw_prompt": "x y", "timestamp": "0"},
        {"request_id": "2", "user_id": "a", "raw_prompt": "x y", "timestamp": "5"},
        {"request_id": "3", "user_id": "a", "raw_prompt": "x y z w", "timestamp": "9"},
    ])
    r = analyze_model_csv(p, FakeEncoder())["apps"][0]
    assert r["lcps"] == [0, 2, 2]                  # miss, full hit, hit first 2
    total_blocks = 2 + 2 + 4
    assert abs(r["ideal_hit_rate"] - sum(r["lcps"]) / total_blocks) < 1e-12


def test_reuse_time_inter_access_gaps(tmp_path):
    p = tmp_path / "d.csv"
    _csv(p, [
        {"request_id": "1", "user_id": "a", "raw_prompt": "x y", "timestamp": "0"},
        {"request_id": "2", "user_id": "a", "raw_prompt": "x y", "timestamp": "100"},
    ])
    q = analyze_model_csv(p, FakeEncoder())["reuse_quantiles"]
    # both blocks reused once, each gap 100
    assert q["count"] == 2
    assert q["p50"] == 100 and q["p90"] == 100 and q["max"] == 100


def test_reuse_time_chronological_even_if_rows_unsorted(tmp_path):
    # rows out of ts order — analyzer must sort so gaps are correct (not negative)
    p = tmp_path / "d.csv"
    _csv(p, [
        {"request_id": "2", "user_id": "a", "raw_prompt": "x y", "timestamp": "100"},
        {"request_id": "1", "user_id": "a", "raw_prompt": "x y", "timestamp": "0"},
    ])
    q = analyze_model_csv(p, FakeEncoder())["reuse_quantiles"]
    assert q["max"] == 100          # not -100
    assert q["count"] == 2


def test_pct_of_total_requests(tmp_path):
    p = tmp_path / "d.csv"
    _csv(p, [
        {"request_id": "1", "user_id": "app1", "raw_prompt": "x", "timestamp": "0"},
        {"request_id": "2", "user_id": "app1", "raw_prompt": "x", "timestamp": "1"},
        {"request_id": "3", "user_id": "app2", "raw_prompt": "x", "timestamp": "2"},
    ])
    apps = {r["app_id"]: r for r in analyze_model_csv(p, FakeEncoder())["apps"]}
    assert apps["app1"]["reqs"] == 2 and abs(apps["app1"]["pct"] - 200 / 3) < 1e-9
    assert apps["app2"]["reqs"] == 1


def test_empty_prompt_counted_lcp_zero(tmp_path):
    p = tmp_path / "d.csv"
    _csv(p, [
        {"request_id": "1", "user_id": "a", "raw_prompt": "", "timestamp": "0"},
        {"request_id": "2", "user_id": "a", "raw_prompt": "x y", "timestamp": "1"},
    ])
    stats = analyze_model_csv(p, FakeEncoder())
    assert stats["model"]["reqs"] == 2
    assert stats["apps"][0]["lcps"] == [0, 0]   # empty→0, then "x y" miss→0


def test_build_html_has_four_sections(tmp_path):
    p = tmp_path / "d.csv"
    _csv(p, [
        {"request_id": "1", "user_id": "tenantA", "raw_prompt": "x y z", "timestamp": "0"},
        {"request_id": "2", "user_id": "tenantA", "raw_prompt": "x y z", "timestamp": "60"},
        {"request_id": "3", "user_id": "tenantB", "raw_prompt": "a b", "timestamp": "30"},
    ])
    stats = analyze_model_csv(p, FakeEncoder())
    meta = {"name": "fake", "block_size": 128, "block_unit": "tokens",
            "chat_mode": "wrap_user", "tokenizer_path": "models/x"}
    out = build_html(stats, meta, "d.csv", "tokens")
    assert "1. 模型级指标" in out
    assert "2. APP 级指标" in out
    assert "3. 模型级 reuse time" in out
    assert "4. 每个用户的 LCP distribution" in out
    assert "tenantA" in out and "tenantB" in out
    assert "<svg" in out                # CDF + LCP histograms rendered


def test_fmt_duration():
    assert fmt_duration(0) == "—"
    assert fmt_duration(None) == "—"
    assert fmt_duration(90) == "1m 30s"
    assert fmt_duration(3600) == "1h"
    assert fmt_duration(90061) == "1d 1h 1m"


class TestEncoderEncodeWithLength:
    def test_byte_encoder_keys_match_and_length_is_bytes(self):
        from lib.prompt_encoder import ByteLevelEncoder
        enc = ByteLevelEncoder(block_size_bytes=4)
        prompt = "héllo world"          # multibyte to check utf-8 length
        keys, n = enc.encode_with_length(prompt)
        assert keys == enc.encode(prompt)
        assert n == len(prompt.encode("utf-8"))
