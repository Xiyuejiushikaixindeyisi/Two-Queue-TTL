"""Tests for lib/hit_rate.py (canonical LCP / pooled hit rate / lcp_histogram)."""
from __future__ import annotations

from lib.hit_rate import lcp, lcp_histogram, percentile_int, pooled_hit_rate


def test_lcp_prefix_run():
    seen = {b"a", b"b", b"d"}
    assert lcp([b"a", b"b", b"c"], seen) == 2     # stops at first miss
    assert lcp([b"x"], seen) == 0
    assert lcp([b"a", b"b"], seen) == 2
    assert lcp([], seen) == 0


def test_lcp_does_not_mutate_seen():
    seen = {b"a"}
    lcp([b"a", b"b"], seen)
    assert seen == {b"a"}


def test_pooled_hit_rate_identical_requests():
    r = pooled_hit_rate([[b"x", b"y", b"z"], [b"x", b"y", b"z"]])
    assert r["total_blocks"] == 6
    assert r["unique_blocks"] == 3
    assert r["hit_blocks"] == 3
    assert r["ideal_hit_rate"] == 0.5


def test_pooled_hit_rate_invariant_hit_equals_total_minus_unique():
    lists = [[b"x", b"y"], [b"x", b"z"], [b"x", b"y", b"w"]]
    r = pooled_hit_rate(lists)
    assert r["hit_blocks"] == r["total_blocks"] - r["unique_blocks"]


def test_pooled_hit_rate_empty():
    r = pooled_hit_rate([])
    assert r["ideal_hit_rate"] == 0.0 and r["total_blocks"] == 0


def test_percentile_int():
    s = [10, 20, 30, 40, 50]
    assert percentile_int(s, 0) == 10
    assert percentile_int(s, 100) == 50
    assert percentile_int(s, 50) == 30
    assert percentile_int([], 50) == 0


def test_lcp_histogram_buckets_quantiles_top10():
    lcps = [0, 0, 0, 5, 5, 30]
    hist, q, top10 = lcp_histogram(lcps)
    assert q["max"] == 30
    assert q["p50"] == percentile_int(sorted(lcps), 50)
    assert sum(b["count"] for b in hist) == len(lcps)
    # top10 sorted by frequency; 0 appears 3× → first
    assert top10[0] == {"lcp_value": 0, "request_count": 3}


def test_lcp_histogram_empty():
    hist, q, top10 = lcp_histogram([])
    assert hist == [] and top10 == [] and q["max"] == 0
