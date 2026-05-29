"""Tests for scripts/dataset_hit_rate.py (folder → per-dataset ideal hit rate).

Uses a fake encoder whose block keys mimic the real chain-hash property (key at
position i encodes the whole prefix), so we can verify the pooled LCP math and
the order-independence invariant (hits == total_blocks - unique_blocks) without
needing transformers / a real tokenizer.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from dataset_hit_rate import analyze_dataset, discover_csvs  # noqa: E402


class FakeEncoder:
    """encode(prompt) → list of position-sensitive prefix keys (mimics chain hash)."""
    kv_bytes_per_token = 100
    block_size_tokens = 1

    def encode(self, prompt: str) -> list[bytes]:
        keys, acc = [], ""
        for word in prompt.split():
            acc += word + "|"
            keys.append(acc.encode())
        return keys

    def encode_with_length(self, prompt: str):
        # token count = number of words (so over-length filter is testable)
        return self.encode(prompt), len(prompt.split())


def _write_csv(path: Path, rows: list[dict], header: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)


HEADER = ["request_id", "user_id", "raw_prompt", "timestamp"]


def test_two_identical_requests_pooled(tmp_path):
    csv_path = tmp_path / "d.csv"
    _write_csv(csv_path, [
        {"request_id": "1", "user_id": "a", "raw_prompt": "x y z", "timestamp": "0"},
        {"request_id": "2", "user_id": "a", "raw_prompt": "x y z", "timestamp": "60"},
    ], HEADER)
    s = analyze_dataset(csv_path, FakeEncoder(), block_size=1)
    assert s["total_blocks"] == 6
    assert s["unique_blocks"] == 3
    assert s["hit_blocks"] == 3
    assert s["ideal_hit_rate"] == 0.5


def test_cross_app_prefix_sharing_counted_when_pooled(tmp_path):
    # Two DIFFERENT apps share the prefix "x" — pooled mode must count that reuse.
    csv_path = tmp_path / "d.csv"
    _write_csv(csv_path, [
        {"request_id": "1", "user_id": "app1", "raw_prompt": "x y", "timestamp": "0"},
        {"request_id": "2", "user_id": "app2", "raw_prompt": "x z", "timestamp": "0"},
    ], HEADER)
    s = analyze_dataset(csv_path, FakeEncoder(), block_size=1)
    # keys: {x|, x|y|} ∪ {x|, x|z|} → unique 3, total 4, hit 1 (the shared "x|")
    assert s["total_blocks"] == 4
    assert s["unique_blocks"] == 3
    assert s["hit_blocks"] == 1
    assert s["n_apps"] == 2


def test_hits_equal_total_minus_unique_invariant(tmp_path):
    # Order-independence proof: hit_blocks == total - unique no matter the row order.
    rows = [
        {"request_id": "1", "user_id": "a", "raw_prompt": "x y", "timestamp": "0"},
        {"request_id": "2", "user_id": "b", "raw_prompt": "x z", "timestamp": "0"},
        {"request_id": "3", "user_id": "a", "raw_prompt": "x y w", "timestamp": "0"},
    ]
    p1 = tmp_path / "fwd.csv"
    p2 = tmp_path / "rev.csv"
    _write_csv(p1, rows, HEADER)
    _write_csv(p2, list(reversed(rows)), HEADER)
    s1 = analyze_dataset(p1, FakeEncoder(), block_size=1)
    s2 = analyze_dataset(p2, FakeEncoder(), block_size=1)
    assert s1["ideal_hit_rate"] == s2["ideal_hit_rate"]
    for s in (s1, s2):
        assert s["hit_blocks"] == s["total_blocks"] - s["unique_blocks"]


def test_app_id_filter(tmp_path):
    csv_path = tmp_path / "d.csv"
    _write_csv(csv_path, [
        {"request_id": "1", "user_id": "app1", "raw_prompt": "x y", "timestamp": "0"},
        {"request_id": "2", "user_id": "app2", "raw_prompt": "q r s t", "timestamp": "0"},
        {"request_id": "3", "user_id": "app1", "raw_prompt": "x y", "timestamp": "0"},
    ], HEADER)
    s = analyze_dataset(csv_path, FakeEncoder(), block_size=1, app_id="app1")
    assert s["reqs"] == 2          # only app1 rows
    assert s["n_apps"] == 1
    assert s["total_blocks"] == 4  # 2 reqs × 2 blocks
    assert s["unique_blocks"] == 2
    assert s["ideal_hit_rate"] == 0.5


def test_empty_prompt_counted_as_req_but_no_blocks(tmp_path):
    csv_path = tmp_path / "d.csv"
    _write_csv(csv_path, [
        {"request_id": "1", "user_id": "a", "raw_prompt": "", "timestamp": "0"},
        {"request_id": "2", "user_id": "a", "raw_prompt": "x y", "timestamp": "0"},
    ], HEADER)
    s = analyze_dataset(csv_path, FakeEncoder(), block_size=1)
    assert s["reqs"] == 2
    assert s["total_blocks"] == 2


def test_gb_per_min_and_missing_ts(tmp_path):
    csv_path = tmp_path / "d.csv"
    _write_csv(csv_path, [
        {"request_id": "1", "user_id": "a", "raw_prompt": "x y", "timestamp": "0"},
        {"request_id": "2", "user_id": "a", "raw_prompt": "x y z", "timestamp": "120"},
    ], HEADER)
    s = analyze_dataset(csv_path, FakeEncoder(), block_size=1)
    # duration = 120s = 2 min; unique=3; kv_bpt=100; block_size=1
    expected = 3 * 1 * 100 / (1024 ** 3) / 2.0
    assert abs(s["avg_gb_per_min"] - expected) < 1e-12

    # all ts identical → duration 0 → no GB/min
    csv2 = tmp_path / "d2.csv"
    _write_csv(csv2, [
        {"request_id": "1", "user_id": "a", "raw_prompt": "x y", "timestamp": "5"},
        {"request_id": "2", "user_id": "a", "raw_prompt": "x y", "timestamp": "5"},
    ], HEADER)
    s2 = analyze_dataset(csv2, FakeEncoder(), block_size=1)
    assert s2["avg_gb_per_min"] is None


def test_chinese_aliases_and_app_col(tmp_path):
    csv_path = tmp_path / "cn.csv"
    _write_csv(csv_path, [
        {"请求ID": "1", "租户ID": "tenantA", "请求参数": "x y", "timestamp": "0"},
        {"请求ID": "2", "租户ID": "tenantB", "请求参数": "x y", "timestamp": "0"},
    ], ["请求ID", "租户ID", "请求参数", "timestamp"])
    s = analyze_dataset(csv_path, FakeEncoder(), block_size=1, app_id="tenantA")
    assert s["reqs"] == 1
    assert s["total_blocks"] == 2


def test_over_length_requests_dropped(tmp_path):
    p = tmp_path / "d.csv"
    _write_csv(p, [
        {"request_id": "1", "user_id": "a", "raw_prompt": "x y z", "timestamp": "0"},        # 3 tok
        {"request_id": "2", "user_id": "a", "raw_prompt": "a b c d e f g", "timestamp": "1"},  # 7 > 4
        {"request_id": "3", "user_id": "a", "raw_prompt": "x y z", "timestamp": "2"},        # 3 tok
    ], HEADER)
    s = analyze_dataset(p, FakeEncoder(), block_size=1, max_input_tokens=4)
    assert s["skipped_over_length"] == 1
    assert s["reqs"] == 2                  # over-length row excluded from reqs
    assert s["total_blocks"] == 6          # only the two 3-block requests
    assert s["unique_blocks"] == 3


def test_no_filter_keeps_all(tmp_path):
    p = tmp_path / "d.csv"
    _write_csv(p, [
        {"request_id": "1", "user_id": "a", "raw_prompt": "a b c d e", "timestamp": "0"},
    ], HEADER)
    s = analyze_dataset(p, FakeEncoder(), block_size=1, max_input_tokens=None)
    assert s["skipped_over_length"] == 0 and s["reqs"] == 1


def test_discover_csvs_dir_and_dedupe(tmp_path):
    (tmp_path / "a.csv").write_text("x", encoding="utf-8")
    (tmp_path / "b.csv").write_text("x", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    # --dir glob + an overlapping explicit file → deduped, still 2 unique
    found = discover_csvs([str(tmp_path / "a.csv")], tmp_path, "*.csv")
    names = sorted(p.name for p in found)
    assert names == ["a.csv", "b.csv"]
