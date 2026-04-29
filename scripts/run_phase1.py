#!/usr/bin/env python3
"""CLI: run a multi-policy experiment from a JSON config file.

Examples
--------
    python scripts/run_phase1.py --config configs/phase1_toy_trace.json
    python scripts/run_phase1.py --config configs/phase1_toy_trace.json --output-dir outputs/my_run
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.config import ExperimentConfig
from sim.experiment import ExperimentRunner
from sim.metrics.reporter import compare_table, print_report


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run a multi-policy KV cache simulation experiment",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--config",
        required=True,
        help="Path to JSON experiment config file",
    )
    p.add_argument(
        "--output-dir",
        default=None,
        help="Override output_dir from config",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    with open(args.config) as f:
        cfg_dict = json.load(f)

    exp_config = ExperimentConfig.from_dict(cfg_dict)
    if args.output_dir:
        exp_config.output_dir = args.output_dir

    print(f"Experiment : {exp_config.name}")
    print(f"Trace      : {exp_config.trace_path}")
    print(f"Output     : {exp_config.output_dir}")
    print(f"Policies   : {[p.policy for p in exp_config.policies]}")
    print()

    runner = ExperimentRunner(exp_config)
    snapshots = runner.run()

    for s in snapshots:
        print_report(s, title=f"{s.policy_name} — capacity={s.cache_capacity}")

    compare_table(snapshots)

    print(f"Results written to: {exp_config.output_dir}/")


if __name__ == "__main__":
    main()
