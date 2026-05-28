"""Canonical reuse_time helpers — single source of truth.

reuse_time = 某 block 距**上次访问**的时间间隔 (秒)。对每个之前见过的 block, 记录
`ts - last_seen[block]`。与 hit_rate (顺序无关) 不同, reuse_time **依赖请求时序**, 调用方
必须按 timestamp 升序喂数据。核心算式: cache_pressure_GB/min × reuse_time_P95(分钟)
≈ 维持命中率所需 cache 容量 (docs/metrics_glossary.md §2.5)。

定义忠实抽取自 per_user_report_analyzer.py / model_report.py, 供 model_report /
app_report 共用。
"""
from __future__ import annotations

import math

from lib.hit_rate import percentile_int


class ReuseTracker:
    """按时序喂 (keys, ts), 累积 block 维度复用间隔 (秒)。"""

    def __init__(self) -> None:
        self.last_seen: dict = {}
        self.gaps: list[int] = []

    def add(self, keys: list, ts: int) -> None:
        """记录这一条请求 (ts 必须 >= 之前所有 add 的 ts)。"""
        for k in keys:
            prev = self.last_seen.get(k)
            if prev is not None:
                self.gaps.append(ts - prev)
            self.last_seen[k] = ts


def reuse_quantiles(reuse_times: list[int]) -> dict:
    """P50/P80/P90/P95/MAX (+avg/count)。"""
    if not reuse_times:
        return {"p50": 0, "p80": 0, "p90": 0, "p95": 0, "max": 0, "avg": 0.0, "count": 0}
    s = sorted(reuse_times)
    return {
        "p50": percentile_int(s, 50),
        "p80": percentile_int(s, 80),
        "p90": percentile_int(s, 90),
        "p95": percentile_int(s, 95),
        "max": s[-1],
        "avg": round(sum(s) / len(s), 2),
        "count": len(s),
    }


def reuse_cdf_points(reuse_times: list[int], n_points: int = 50) -> list[dict]:
    """Log-spaced CDF 采样点 [{t_seconds, cumulative_pct}] (供 svg_cdf_log_x)。"""
    if not reuse_times:
        return []
    s = sorted(reuse_times)
    n = len(s)
    max_rt = s[-1]
    if max_rt <= 1:
        grid = [0, max_rt]
    else:
        log_max = math.log10(max_rt)
        grid = sorted({0} | {
            max(1, int(round(10 ** (i * log_max / (n_points - 1))))) for i in range(n_points)
        })
    pts = []
    for t in grid:
        cnt = sum(1 for rt in s if rt <= t)
        pts.append({"t_seconds": t, "cumulative_pct": round(cnt / n * 100, 3)})
    return pts


def fmt_duration(seconds: int | None) -> str:
    """秒 → 人类可读时长 (d/h/m/s)。0 或 None → '—'。"""
    if not seconds or seconds <= 0:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s and not d:
        parts.append(f"{s}s")
    return " ".join(parts) or "0s"
