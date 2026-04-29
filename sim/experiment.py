"""ExperimentRunner — runs a multi-policy experiment from a JSON config file.

Usage
-----
    config = ExperimentConfig.from_dict(json.load(open("configs/phase1.json")))
    runner = ExperimentRunner(config)
    runner.run()        # writes summary.json + comparison.json to output_dir
"""
from __future__ import annotations

import json
import os
from typing import List

from .config import ExperimentConfig
from .io.trace_loader import load_trace
from .metrics.collector import MetricsSnapshot
from .metrics.reporter import to_json
from .runner import SimulationRunner


class ExperimentRunner:
    """Runs all policies in an ExperimentConfig and writes output files."""

    def __init__(self, config: ExperimentConfig) -> None:
        self._config = config

    def run(self) -> List[MetricsSnapshot]:
        cfg = self._config
        trace = load_trace(cfg.trace_path)

        sim_configs = [p.to_sim_config() for p in cfg.policies]
        snapshots = SimulationRunner.compare(trace, sim_configs)

        os.makedirs(cfg.output_dir, exist_ok=True)
        self._write_summary(snapshots)
        self._write_comparison(snapshots)
        return snapshots

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_summary(self, snapshots: List[MetricsSnapshot]) -> None:
        path = os.path.join(self._config.output_dir, "summary.json")
        payload = {
            "experiment": self._config.name,
            "description": self._config.description,
            "trace_path": self._config.trace_path,
            "results": [json.loads(to_json(s)) for s in snapshots],
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)

    def _write_comparison(self, snapshots: List[MetricsSnapshot]) -> None:
        path = os.path.join(self._config.output_dir, "comparison.json")
        metrics = [
            "prefix_block_hit_rate",
            "saved_prefill_tokens",
            "eviction_count",
            "hot_prefix_eviction_count",
            "evicted_before_next_hit_count",
            "protected_eviction_count",
            "probation_eviction_count",
            "protected_pollution_rate",
            "promotion_count",
        ]
        rows = []
        for metric in metrics:
            row = {"metric": metric}
            for s in snapshots:
                row[s.policy_name] = getattr(s, metric)
            rows.append(row)
        with open(path, "w") as f:
            json.dump(rows, f, indent=2)
