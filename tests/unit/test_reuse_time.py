"""Tests for lib/reuse_time.py (tracker + quantiles + CDF + duration)."""
from __future__ import annotations

from lib.reuse_time import ReuseTracker, fmt_duration, reuse_cdf_points, reuse_quantiles


def test_tracker_records_inter_access_gaps():
    t = ReuseTracker()
    t.add([b"a", b"b"], 0)        # first sight → no gap
    t.add([b"a", b"b"], 100)      # each reused → gap 100
    assert t.gaps == [100, 100]


def test_tracker_new_block_no_gap():
    t = ReuseTracker()
    t.add([b"a"], 0)
    t.add([b"a", b"b"], 50)       # a→gap 50, b→new, no gap
    assert t.gaps == [50]


def test_reuse_quantiles_has_p90():
    q = reuse_quantiles([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
    assert set(q) == {"p50", "p80", "p90", "p95", "max", "avg", "count"}
    assert q["max"] == 100 and q["count"] == 10
    assert q["p90"] >= q["p80"]


def test_reuse_quantiles_empty():
    q = reuse_quantiles([])
    assert q == {"p50": 0, "p80": 0, "p90": 0, "p95": 0, "max": 0, "avg": 0.0, "count": 0}


def test_reuse_cdf_points_monotonic_and_bounded():
    pts = reuse_cdf_points([1, 10, 100, 1000])
    assert pts[0]["cumulative_pct"] >= 0
    assert pts[-1]["cumulative_pct"] == 100.0
    pcts = [p["cumulative_pct"] for p in pts]
    assert pcts == sorted(pcts)            # non-decreasing
    assert reuse_cdf_points([]) == []


def test_fmt_duration():
    assert fmt_duration(0) == "—"
    assert fmt_duration(None) == "—"
    assert fmt_duration(90) == "1m 30s"
    assert fmt_duration(3600) == "1h"
    assert fmt_duration(90061) == "1d 1h 1m"
