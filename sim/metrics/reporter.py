"""Human-readable and machine-readable output for MetricsSnapshot."""
from __future__ import annotations

import csv
import io
import json
from typing import List, Optional

from .collector import MetricsSnapshot


_REPORT_FIELDS = [
    ("policy",                      "policy_name",                      "s",    None),
    ("cache_capacity",              "cache_capacity",                   "d",    None),
    ("total_requests",              "total_requests",                   "d",    None),
    ("total_blocks_requested",      "total_blocks_requested",           "d",    None),
    ("total_blocks_hit",            "total_blocks_hit",                 "d",    None),
    ("prefix_block_hit_rate",       "prefix_block_hit_rate",            ".4f",  None),
    ("saved_prefill_tokens",        "saved_prefill_tokens",             "d",    None),
    ("eviction_count",              "eviction_count",                   "d",    None),
    ("hot_prefix_eviction_count",   "hot_prefix_eviction_count",        "d",    None),
    ("evicted_before_next_hit",     "evicted_before_next_hit_count",    "d",    None),
    ("protected_evictions",         "protected_eviction_count",         "d",    None),
    ("probation_evictions",         "probation_eviction_count",         "d",    None),
    ("protected_pollution_rate",    "protected_pollution_rate",         ".4f",  None),
    ("promotion_count",             "promotion_count",                  "d",    None),
]


def print_report(snapshot: MetricsSnapshot, title: Optional[str] = None) -> None:
    """Print a formatted table of all metrics to stdout."""
    if title:
        print(f"\n{'='*60}")
        print(f"  {title}")
        print(f"{'='*60}")
    for label, attr, fmt, _ in _REPORT_FIELDS:
        value = getattr(snapshot, attr)
        print(f"  {label:<32} {value:{fmt}}")
    if snapshot.gap_closed_ratio is not None:
        print(f"  {'gap_closed_ratio':<32} {snapshot.gap_closed_ratio:.4f}")
    print()


def to_json(snapshot: MetricsSnapshot) -> str:
    return json.dumps(snapshot.to_dict(), indent=2)


def to_csv_row(snapshot: MetricsSnapshot) -> str:
    """Return one CSV line (with header) for the snapshot."""
    d = snapshot.to_dict()
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(d.keys()))
    writer.writeheader()
    writer.writerow(d)
    return buf.getvalue()


def compare_table(snapshots: List[MetricsSnapshot]) -> None:
    """Print a side-by-side comparison table for multiple policy runs."""
    if not snapshots:
        return
    col_w = 18
    metrics = [
        ("prefix_block_hit_rate",          ".4f"),
        ("gap_closed_ratio",               ".4f"),
        ("saved_prefill_tokens",           "d"),
        ("eviction_count",                 "d"),
        ("hot_prefix_eviction_count",      "d"),
        ("evicted_before_next_hit_count",  "d"),
        ("protected_pollution_rate",       ".4f"),
        ("promotion_count",                "d"),
    ]
    header = f"{'metric':<36}" + "".join(
        f"{s.policy_name:>{col_w}}" for s in snapshots
    )
    print("\n" + "=" * len(header))
    print(header)
    print("-" * len(header))
    for attr, fmt in metrics:
        row = f"{attr:<36}"
        for s in snapshots:
            val = getattr(s, attr)
            if val is None:
                row += f"{'—':>{col_w}}"
            else:
                row += format(val, fmt).rjust(col_w)
        print(row)
    print("=" * len(header) + "\n")
