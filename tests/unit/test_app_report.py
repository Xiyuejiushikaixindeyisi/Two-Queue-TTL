"""Tests for scripts/app_report.py (APP-level report from CSV+app-id or txt folder).

Core analysis (analyze_base / record building / HTML composition) is tested with
synthetic records and a fake tokenizer so no real GLM tokenizer is needed. The
chain-finding itself is covered by multi_chain_finder's own tests; here we only
verify the wiring (records → trie → forest → metrics) and the 5/4-section split.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app_report import (  # noqa: E402
    analyze_base,
    base_forest,
    build_html,
    build_records_from_csv,
    build_records_from_txt,
    decode_chain_prefixes,
    variant_metrics,
)
from lib.prompt_rewrite import DEFAULT_PATTERNS  # noqa: E402
from per_user_report_4variant import VARIANTS  # noqa: E402

CHAIN_KW = {"mc_branch_thr": 0.5, "mc_cov_thr": 0.05,
            "min_chain_length": 2, "min_chain_coverage": 0.0, "max_chains": 10}


def _rec(rid, ts, keys, ntok):
    return {"request_id": str(rid), "ts": ts, "input_length": ntok,
            "hash_ids": {"base": list(keys)}}


def test_analyze_base_hit_rate_lcp_reuse():
    recs = [
        _rec(1, 0, [b"A", b"B", b"C"], 300),
        _rec(2, 100, [b"A", b"B", b"D"], 300),
    ]
    b = analyze_base(recs)
    assert b["total_blocks"] == 6
    assert b["unique_blocks"] == 4          # A,B,C,D
    assert abs(b["ideal_hit_rate"] - 2 / 6) < 1e-12
    assert b["lcps"] == [0, 2]
    assert b["avg_len"] == 300.0
    assert b["span_seconds"] == 100
    q = b["reuse_quantiles"]
    assert q["count"] == 2 and q["p50"] == 100 and q["p90"] == 100 and q["max"] == 100


def test_analyze_base_reuse_sorts_by_ts():
    # rows given out of order — reuse gaps must be positive
    recs = [
        _rec(2, 100, [b"A", b"B"], 200),
        _rec(1, 0, [b"A", b"B"], 200),
    ]
    b = analyze_base(recs)
    assert b["reuse_quantiles"]["max"] == 100      # not -100
    assert b["ideal_hit_rate"] == 0.5              # order-independent


def test_analyze_base_no_ts_means_no_reuse_and_no_span():
    recs = [_rec(1, None, [b"A", b"B"], 200), _rec(2, None, [b"A", b"B"], 200)]
    b = analyze_base(recs)
    assert b["reuse_quantiles"]["count"] == 0
    assert b["span_seconds"] is None
    assert b["ideal_hit_rate"] == 0.5              # hit_rate independent of ts


def test_variant_metrics_keys_and_base_consistency():
    recs = [
        _rec(1, 0, [b"A", b"B", b"C"], 300),
        _rec(2, 1, [b"A", b"B", b"C"], 300),
        _rec(3, 2, [b"A", b"B", b"C"], 300),
    ]
    m = variant_metrics(recs, "base", CHAIN_KW)
    assert set(m) == {"ideal_hit_rate", "chain_count", "top_chain_length", "top_chain_coverage"}
    # base hit rate via trie must match the sequential analyze_base
    assert abs(m["ideal_hit_rate"] - analyze_base(recs)["ideal_hit_rate"]) < 1e-9


def test_base_forest_finds_shared_prefix_chain():
    # 3 requests share a 4-block prefix → a chain should be detected
    shared = [b"p1", b"p2", b"p3", b"p4"]
    recs = [
        _rec(1, 0, shared + [b"x1"], 600),
        _rec(2, 1, shared + [b"x2"], 600),
        _rec(3, 2, shared + [b"x3"], 600),
    ]
    forest, n_req = base_forest(recs, CHAIN_KW)
    assert n_req == 3
    assert isinstance(forest.get("chains"), list)
    assert len(forest["chains"]) >= 1
    top = max(forest["chains"], key=lambda c: c["coverage_count"])
    assert top["chain_length"] >= 4               # the shared prefix


def test_build_records_from_txt(tmp_path):
    (tmp_path / "a.txt").write_text("hello world foo", encoding="utf-8")
    (tmp_path / "b.txt").write_text("hello world bar", encoding="utf-8")

    class FakeEncoder:
        block_unit = "tokens"

        def encode_with_length(self, prompt):
            keys, acc = [], ""
            for w in prompt.split():
                acc += w + "|"
                keys.append(acc.encode())
            return keys, len(prompt.split())

    recs, rid_to_tokens, n_over = build_records_from_txt(tmp_path, FakeEncoder(), block_size=128)
    assert len(recs) == 2
    assert all(set(r["hash_ids"]) == {"base"} for r in recs)
    assert all(r["ts"] is None for r in recs)
    assert recs[0]["input_length"] == 3
    assert rid_to_tokens == {}        # FakeEncoder has no .tokenizer → no decode tokens
    assert n_over == 0


class FakeTok:
    def apply_chat_template(self, messages, tools=None, tokenize=False, add_generation_prompt=True):
        txt = "".join(m.get("content", "") for m in messages if isinstance(m, dict))
        return txt + f"|tools={len(tools or [])}"

    def encode(self, text, add_special_tokens=False):
        return [ord(c) % 251 for c in text]

    def decode(self, token_ids):
        return f"DECODED[{len(token_ids)}]"


def _write_csv(path, rows, header):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_build_records_from_csv_filters_app_and_counts_skips(tmp_path):
    p = tmp_path / "t.csv"
    body = '{"messages":[{"role":"user","content":"hello there"}],"tools":[]}'
    _write_csv(p, [
        {"请求ID": "1", "租户ID": "app1", "请求参数": body, "timestamp": "0"},
        {"请求ID": "2", "租户ID": "app2", "请求参数": body, "timestamp": "5"},   # filtered out
        {"请求ID": "3", "租户ID": "app1", "请求参数": "not-json", "timestamp": "1"},
        {"请求ID": "4", "租户ID": "app1", "请求参数": '{"messages":[]}', "timestamp": "2"},
    ], ["请求ID", "租户ID", "请求参数", "timestamp"])

    recs, counts, rid_to_tokens = build_records_from_csv(
        p, "app1", FakeTok(), DEFAULT_PATTERNS, VARIANTS, block_size=8, app_col=None)
    assert counts["matched"] == 3            # 3 app1 rows
    assert counts["no_json"] == 1
    assert counts["no_messages"] == 1
    assert len(recs) == 1                    # only the valid app1 row
    assert set(recs[0]["hash_ids"]) == set(VARIANTS)
    assert recs[0]["ts"] == 0
    # base token_ids retained for the valid row (for §5 decode)
    assert rid_to_tokens[recs[0]["request_id"]]
    assert len(rid_to_tokens) == 1


def test_build_html_section_split():
    recs = [_rec(1, 0, [b"A", b"B"], 200), _rec(2, 10, [b"A", b"B"], 200)]
    b = analyze_base(recs)
    forest, n_req = base_forest(recs, CHAIN_KW)
    meta = {"name": "fake", "block_size": 128, "block_unit": "tokens"}
    pv = {v: variant_metrics(recs, "base", CHAIN_KW) for v in VARIANTS}

    with_4v = build_html("app1", b, meta, "tokens", pv, forest, n_req, "x.csv")
    assert "1. APP 指标" in with_4v
    assert "2. APP reuse time" in with_4v
    assert "3. per-request LCP distribution" in with_4v
    assert "4. 4 变体对比" in with_4v
    assert "5. chain forest (base)" in with_4v

    no_4v = build_html("app1", b, meta, "tokens", None, forest, n_req, "txtdir")
    assert "4. 4 变体对比" not in no_4v       # txt path omits §4
    assert "省略 4 变体块" in no_4v


def _forest_with_sample(records):
    from lib.chains import build_trie, find_chain_forest
    root, _, _ = build_trie(records, "base")
    return find_chain_forest(root, mc_branch_thr=0.5, mc_cov_thr=0.05,
                             min_chain_length=2, min_chain_coverage=0.0, max_chains=10)


def test_decode_chain_prefixes_uses_sample_request():
    # 3 requests share a 3-block prefix → one chain; decode its prefix
    shared = [b"p1", b"p2", b"p3"]
    recs = [_rec(i, i, shared + [f"x{i}".encode()], 400) for i in range(3)]
    forest = _forest_with_sample(recs)
    rid_to_tokens = {str(i): list(range(500)) for i in range(3)}  # any sample works

    class Tok:
        def decode(self, ids):
            return f"DECODED[{len(ids)}]"

    decoded = decode_chain_prefixes(forest, rid_to_tokens, Tok(), block_size=128)
    cid = forest["chains"][0]["chain_id"]
    assert cid in decoded
    assert decoded[cid]["text"].startswith("DECODED[")
    # no tokenizer → empty
    assert decode_chain_prefixes(forest, rid_to_tokens, None, 128) == {}


def test_decode_max_blocks_truncation():
    shared = [f"b{i}".encode() for i in range(6)]
    recs = [_rec(i, i, shared, 800) for i in range(3)]
    forest = _forest_with_sample(recs)
    rid_to_tokens = {str(i): list(range(1000)) for i in range(3)}

    class Tok:
        def decode(self, ids):
            return "x" * len(ids)

    decoded = decode_chain_prefixes(forest, rid_to_tokens, Tok(), block_size=128, max_blocks=2)
    d = decoded[forest["chains"][0]["chain_id"]]
    assert d["blocks"] == 2 and d["truncated"] is True
    assert len(d["text"]) == 2 * 128


def test_build_html_renders_decoded_details():
    recs = [_rec(i, i, [b"p1", b"p2", b"p3"] + [f"x{i}".encode()], 400) for i in range(3)]
    b = analyze_base(recs)
    forest = _forest_with_sample(recs)
    n_req = 3
    meta = {"name": "fake", "block_size": 128, "block_unit": "tokens"}

    class Tok:
        def decode(self, ids):
            return "SYSTEM PROMPT TEXT"

    decoded = decode_chain_prefixes(forest, {str(i): list(range(500)) for i in range(3)},
                                    Tok(), block_size=128)
    out = build_html("app1", b, meta, "tokens", None, forest, n_req, "x", decoded)
    assert "<details class=\"chain\">" in out
    assert "SYSTEM PROMPT TEXT" in out
    assert "原始内容" in out


def test_build_records_from_csv_drops_over_length(tmp_path):
    # FakeTok token count = len(rendered text) = len(content)+len("|tools=0")
    p = tmp_path / "t.csv"
    short = '{"messages":[{"role":"user","content":"hi"}],"tools":[]}'
    longb = '{"messages":[{"role":"user","content":"' + "x" * 100 + '"}],"tools":[]}'
    _write_csv(p, [
        {"请求ID": "1", "租户ID": "app1", "请求参数": short, "timestamp": "0"},
        {"请求ID": "2", "租户ID": "app1", "请求参数": longb, "timestamp": "1"},
    ], ["请求ID", "租户ID", "请求参数", "timestamp"])
    recs, counts, rid_to_tokens = build_records_from_csv(
        p, "app1", FakeTok(), DEFAULT_PATTERNS, VARIANTS, block_size=8, app_col=None,
        max_input_tokens=20)
    assert counts["over_length"] == 1     # the 100-char one
    assert len(recs) == 1                  # only the short one kept
    assert len(rid_to_tokens) == 1


def test_build_records_from_txt_drops_over_length(tmp_path):
    (tmp_path / "a.txt").write_text("one two", encoding="utf-8")     # 2 tok
    (tmp_path / "b.txt").write_text("a b c d e", encoding="utf-8")   # 5 tok > 3

    class FakeEncoder:
        block_unit = "tokens"

        def encode_with_length(self, prompt):
            keys, acc = [], ""
            for w in prompt.split():
                acc += w + "|"
                keys.append(acc.encode())
            return keys, len(prompt.split())

    recs, rid_to_tokens, n_over = build_records_from_txt(
        tmp_path, FakeEncoder(), block_size=128, max_input_tokens=3)
    assert n_over == 1
    assert len(recs) == 1
