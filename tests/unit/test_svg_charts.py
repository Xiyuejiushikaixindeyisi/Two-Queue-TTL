"""Smoke tests for lib/svg_charts.py (inline-SVG chart helpers)."""
from __future__ import annotations

from lib.svg_charts import svg_cdf_log_x, svg_histogram


def test_cdf_empty_returns_placeholder():
    assert "empty" in svg_cdf_log_x([], {})


def test_cdf_renders_svg_with_markers():
    pts = [{"t_seconds": 1, "cumulative_pct": 0.0},
           {"t_seconds": 100, "cumulative_pct": 50.0},
           {"t_seconds": 1000, "cumulative_pct": 100.0}]
    out = svg_cdf_log_x(pts, {"p50": 100, "p80": 500, "p95": 900})
    assert out.startswith("<svg") and "polyline" in out
    assert "p50=100s" in out


def test_histogram_empty_and_render():
    assert svg_histogram([], "x") == ""
    hist = [{"lcp_low": 0, "lcp_high": 4, "count": 3},
            {"lcp_low": 5, "lcp_high": 9, "count": 1}]
    out = svg_histogram(hist, "my <title>")
    assert out.startswith("<svg") and "<rect" in out
    assert "my &lt;title&gt;" in out          # title escaped
