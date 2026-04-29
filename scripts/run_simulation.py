#!/usr/bin/env python3
"""CLI entry point for running offline KV cache eviction simulations.

Examples
--------
# LRU baseline
python scripts/run_simulation.py \\
    --trace data/trace.csv \\
    --policy lru \\
    --capacity 10000

# Two-Queue TTL with custom TTL params
python scripts/run_simulation.py \\
    --trace data/trace.csv \\
    --policy two_queue_ttl \\
    --capacity 10000 \\
    --base-ttl 46 \\
    --extended-ttl 255 \\
    --protected-ratio 0.3

# Compare all policies and output CSV
python scripts/run_simulation.py \\
    --trace data/trace.csv \\
    --compare \\
    --capacity 10000 \\
    --output results/run1.csv
"""
from __future__ import annotations

import argparse
import sys
import os

# Allow running as a script from project root without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.config import SimConfig, TwoQueueTTLConfig
from sim.io.raw_trace_loader import load_raw_trace
from sim.io.trace_loader import load_trace
from sim.metrics.reporter import compare_table, print_report, to_csv_row
from sim.policies import POLICY_REGISTRY
from sim.runner import SimulationRunner


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="KV cache eviction offline simulation",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--trace", required=True, help="Path to trace file (.csv or .jsonl)")
    p.add_argument("--raw", action="store_true",
                   help="Input CSV has raw_prompt column (request_id,user_id,raw_prompt,timestamp). "
                        "Tokenization is performed automatically.")
    p.add_argument(
        "--policy",
        default="lru",
        choices=list(POLICY_REGISTRY),
        help="Eviction policy to run (ignored when --compare is set)",
    )
    p.add_argument("--capacity", type=int, required=True, help="Cache block capacity")
    p.add_argument("--block-size", type=int, default=128, help="Tokens per block")
    p.add_argument("--base-ttl", type=float, default=46.0, help="Two-Queue base TTL (s)")
    p.add_argument("--extended-ttl", type=float, default=255.0, help="Two-Queue extended TTL (s)")
    p.add_argument("--protected-ratio", type=float, default=0.30, help="Protected queue capacity fraction")
    p.add_argument("--promotion-threshold", type=int, default=2, help="hit_count threshold for promotion")
    p.add_argument("--registry", default=None, help="Path to offline block registry file")
    p.add_argument(
        "--compare",
        action="store_true",
        help="Run all registered policies and print comparison table",
    )
    p.add_argument("--output", default=None, help="Write CSV results to this file")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    print(f"Loading trace: {args.trace}")
    if args.raw:
        trace = load_raw_trace(args.trace, block_size=args.block_size)
    else:
        trace = load_trace(args.trace)
    print(f"  {len(trace)} records loaded")

    ttl_cfg = TwoQueueTTLConfig(
        base_ttl=args.base_ttl,
        extended_ttl=args.extended_ttl,
        promotion_threshold=args.promotion_threshold,
        protected_ratio=args.protected_ratio,
    )

    if args.compare:
        configs = [
            SimConfig(
                cache_capacity=args.capacity,
                block_size=args.block_size,
                policy=name,
                two_queue_ttl=ttl_cfg,
                registry_path=args.registry,
            )
            for name in POLICY_REGISTRY
        ]
        snapshots = SimulationRunner.compare(trace, configs)
        compare_table(snapshots)
        if args.output:
            with open(args.output, "w") as f:
                for s in snapshots:
                    f.write(to_csv_row(s))
            print(f"Results written to {args.output}")
    else:
        config = SimConfig(
            cache_capacity=args.capacity,
            block_size=args.block_size,
            policy=args.policy,
            two_queue_ttl=ttl_cfg,
            registry_path=args.registry,
        )
        snapshot = SimulationRunner(config).run(trace)
        print_report(snapshot, title=f"{args.policy} — capacity={args.capacity}")
        if args.output:
            with open(args.output, "w") as f:
                f.write(to_csv_row(snapshot))
            print(f"Results written to {args.output}")


if __name__ == "__main__":
    main()
