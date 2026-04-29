#!/usr/bin/env python3
"""Generate a synthetic trace calibrated to Qwen-V3.5-27B-64K characteristics.

Purpose
-------
Enables a full end-to-end pipeline test (capacity sweep + gap_closed_ratio)
without committing real production data.  Parameters are calibrated so that
the ideal hit rate (Infinite Cache) and reuse-time distribution reproduce
the key properties of the Qwen-V3.5-27B-64K production trace:

  Production (real)          Synthetic (target)
  ─────────────────          ──────────────────
  ideal_hit_rate ≈ 82%       ≈ 80–85%
  reuse_time p80 ≈ 46 s      controlled by --reuse-time-p80
  common prefix coverage 68% controlled by --prefix-coverage
  requests: 8,755            controlled by --num-requests (default 2000)
  unique block IDs: 1.7M     scaled proportionally

Trace structure
---------------
Every request is composed of three concatenated block sequences:

  [common_prefix?] + [session_content] + [request_tail]

  common_prefix  (--prefix-blocks, ~50 blocks)
      Shared system-prompt / Agent skills.  Included by --prefix-coverage
      fraction of requests.  These blocks have the highest hit_count and
      are the primary target of the Two-Queue TTL registry mechanism.

  session_content  (--session-blocks, ~40 blocks)
      Accumulated conversation history within a user session.  All requests
      in the same session share the SAME session_content.  This produces
      the high within-session reuse that drives Infinite Cache hit rate.
      The number of blocks grows slightly each request (multi-turn).

  request_tail  (--tail-blocks, ~5 blocks)
      New user input that is unique to each request.  These blocks will
      never be reused (reuse_distance = ∞), so they should be evicted
      first by any reasonable policy.

Session model
-------------
Users arrive as a Poisson process.  Each user makes between min_turns and
max_turns requests within a session.  The time between consecutive requests
within a session is drawn from a log-normal distribution calibrated to
reproduce F13 reuse_time p50 ≈ 17 s and p80 ≈ 46 s.

Output
------
CSV with columns: timestamp, model_id, user_id, request_type, input_length, hash_ids
Compatible with sim.io.trace_loader.load_trace() directly.

Expected behavior on the clean (no-burst) trace
-------------------------------------------------
On a clean trace (default settings, no --burst-fraction), two_queue_ttl
will show a slightly NEGATIVE gap_closed_ratio vs LRU.  This is expected:

  * The 50 common prefix blocks (accessed ~28× each by 71.5% of requests)
    are too hot for LRU to ever evict — LRU's recency naturally protects them.
    two_queue_ttl's Protected queue provides no additional protection here.
  * The base_ttl=46 s occasionally evicts blocks before their next session
    turn (20% of gaps exceed p80), creating a small TTL penalty.

The production trace (2562 prefix blocks, ~2.3 accesses each) is the target:
LRU fails to protect large prefix chains under session competition → two_queue_ttl
shows positive gap_closed_ratio.  The synthetic trace validates the pipeline;
use --burst-fraction + --burst-blocks to see the policy advantage at small
capacities where burst pollution is close to cache size.

Example
-------
    # Default pipeline validation (clean trace)
    python scripts/generate_synthetic_trace.py \\
        --output data/trace1_synthetic.csv \\
        --num-requests 2000 \\
        --seed 42

    # Verify ideal hit rate ≈ 80-85%
    python scripts/analyze_capacity_sweep.py \\
        --trace data/trace1_synthetic.csv \\
        --capacities 500 1000 2000 4000 8000 \\
        --base-ttl 46 --extended-ttl 255 \\
        --infinite-cache \\
        --output-dir outputs/trace1_synthetic \\
        --plot

    # With cache-pollution bursts (shows two_queue_ttl advantage at small capacities)
    python scripts/generate_synthetic_trace.py \\
        --output data/trace1_burst.csv \\
        --burst-fraction 0.10 --burst-blocks 80 --seed 42
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import math
import os
import random
import sys
from dataclasses import dataclass, field
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Block ID helpers
# ---------------------------------------------------------------------------

def _block_id(tag: str) -> str:
    """Deterministic 16-char hex block ID from a human-readable tag."""
    return hashlib.sha256(tag.encode()).hexdigest()[:16]


def _common_prefix_blocks(n: int) -> List[str]:
    """Fixed common prefix: same blocks across all requests that use it."""
    return [_block_id(f"sys_prompt:block_{i:04d}") for i in range(n)]


def _session_content_blocks(user_id: int, session_id: int, n_blocks: int) -> List[str]:
    """Session content: same blocks for every request within a session."""
    return [_block_id(f"user:{user_id}:session:{session_id}:block_{i:03d}")
            for i in range(n_blocks)]


def _request_tail_blocks(user_id: int, session_id: int, turn: int, n: int) -> List[str]:
    """Per-request tail: unique, never reused."""
    return [_block_id(f"user:{user_id}:session:{session_id}:turn:{turn}:tail_{i}")
            for i in range(n)]


def _burst_blocks(burst_id: int, n: int) -> List[str]:
    """Burst request: large one-time unique blocks, never reused.

    Models one-time API users (RAG document ingestion, analytics queries)
    that flood the cache with unique content.  These are the "cache pollution"
    events that two_queue_ttl's probation queue is designed to absorb.
    """
    return [_block_id(f"burst:{burst_id}:block_{i:04d}") for i in range(n)]


# ---------------------------------------------------------------------------
# Timing model
# ---------------------------------------------------------------------------

def _sample_inter_request_time(rng: random.Random, p80: float) -> float:
    """Sample within-session inter-request interval calibrated to reuse_time.

    Uses a log-normal distribution so that:
      - p50 ≈ p80 / e  (≈ 17 s when p80=46 s)
      - p80 ≈ p80 parameter
      - long-tail matches F13 CDF shape
    """
    # log-normal: mu = ln(p50), sigma such that P80 hits target
    p50 = p80 / math.e
    sigma = (math.log(p80) - math.log(p50)) / 0.842  # z_{0.8} = 0.842
    mu = math.log(p50)
    return rng.lognormvariate(mu, sigma)


# ---------------------------------------------------------------------------
# Session + request generation
# ---------------------------------------------------------------------------

@dataclass
class Session:
    user_id: int
    session_id: int
    start_time: float
    n_turns: int
    n_session_blocks: int
    use_prefix: bool
    prefix_blocks: List[str]
    session_blocks: List[str]
    rng: random.Random = field(repr=False)

    def generate_requests(self, reuse_time_p80: float, tail_blocks: int) -> List[dict]:
        rows = []
        t = self.start_time
        for turn in range(self.n_turns):
            tail = _request_tail_blocks(self.user_id, self.session_id, turn, tail_blocks)
            hash_ids: List[str] = []
            if self.use_prefix:
                hash_ids.extend(self.prefix_blocks)
            hash_ids.extend(self.session_blocks)
            hash_ids.extend(tail)
            rows.append({
                "timestamp": round(t, 3),
                "model_id": "default",
                "user_id": f"u{self.user_id:05d}",
                "request_type": "prefill",
                "input_length": len(hash_ids) * 128,
                "hash_ids": "|".join(hash_ids),
                "output_length": 0,
                "turn": turn,
            })
            if turn < self.n_turns - 1:
                t += _sample_inter_request_time(self.rng, reuse_time_p80)
        return rows


# ---------------------------------------------------------------------------
# Trace generator
# ---------------------------------------------------------------------------

def generate(
    num_requests: int,
    num_users: int,
    prefix_blocks: int,
    prefix_coverage: float,
    session_blocks: int,
    tail_blocks: int,
    min_turns: int,
    max_turns: int,
    reuse_time_p80: float,
    time_span: float,
    seed: int,
    burst_fraction: float = 0.0,
    burst_blocks: int = 0,
) -> List[dict]:
    rng = random.Random(seed)

    common_prefix = _common_prefix_blocks(prefix_blocks)

    # Estimate sessions needed: avg turns = (min+max)/2
    avg_turns = (min_turns + max_turns) / 2
    n_sessions_estimate = int(num_requests / avg_turns) + 1

    # Session start times: uniformly spread with jitter
    session_starts = sorted(
        rng.uniform(0, time_span * 0.9) for _ in range(n_sessions_estimate)
    )

    # User pool — power-law distribution for user popularity (some users very frequent)
    def sample_user() -> int:
        # Zipf-like: user_id biased toward lower indices
        raw = int(abs(rng.gauss(0, num_users / 4)))
        return raw % num_users

    all_rows: List[dict] = []
    session_counter = 0

    for start_t in session_starts:
        if len(all_rows) >= num_requests:
            break

        uid = sample_user()
        sid = session_counter
        session_counter += 1

        n_turns = rng.randint(min_turns, max_turns)
        use_prefix = rng.random() < prefix_coverage

        sess = Session(
            user_id=uid,
            session_id=sid,
            start_time=start_t,
            n_turns=n_turns,
            n_session_blocks=session_blocks,
            use_prefix=use_prefix,
            prefix_blocks=common_prefix,
            session_blocks=_session_content_blocks(uid, sid, session_blocks),
            rng=rng,
        )
        all_rows.extend(sess.generate_requests(reuse_time_p80, tail_blocks))

    # Inject burst requests (cache-pollution events from one-time API users)
    if burst_fraction > 0 and burst_blocks > 0:
        n_bursts = int(num_requests * burst_fraction)
        for bid in range(n_bursts):
            t = rng.uniform(0, time_span)
            blocks = _burst_blocks(bid, burst_blocks)
            all_rows.append({
                "timestamp": round(t, 3),
                "model_id": "default",
                "user_id": f"burst_{bid:05d}",
                "request_type": "prefill",
                "input_length": len(blocks) * 128,
                "hash_ids": "|".join(blocks),
                "output_length": 0,
                "turn": 0,
            })

    # Trim to target size, sort by timestamp
    all_rows.sort(key=lambda r: r["timestamp"])
    all_rows = all_rows[:num_requests]

    return all_rows


# ---------------------------------------------------------------------------
# Stats summary
# ---------------------------------------------------------------------------

def print_stats(rows: List[dict], prefix_blocks: int) -> None:
    import statistics
    n = len(rows)
    block_counts = [len(r["hash_ids"].split("|")) for r in rows]
    unique_blocks = len({b for r in rows for b in r["hash_ids"].split("|")})
    unique_users = len({r["user_id"] for r in rows})
    timestamps = [r["timestamp"] for r in rows]
    time_span = max(timestamps) - min(timestamps)

    # Count how many requests start with the common prefix (block 0)
    common_prefix_block0 = hashlib.sha256(b"sys_prompt:block_0000").hexdigest()[:16]
    with_prefix = sum(1 for r in rows if r["hash_ids"].startswith(common_prefix_block0))

    print(f"\n── Synthetic Trace Statistics ─────────────────────────────────────")
    print(f"  requests              : {n:,}")
    print(f"  time span             : {time_span:.1f} s")
    print(f"  unique users          : {unique_users:,}")
    print(f"  unique block IDs      : {unique_blocks:,}")
    print(f"  avg blocks / request  : {statistics.mean(block_counts):.1f}")
    print(f"  max blocks / request  : {max(block_counts):,}")
    print(f"  reuse potential       : {sum(block_counts)/unique_blocks:.2f}x")
    print(f"  requests with prefix  : {with_prefix:,} ({with_prefix/n*100:.1f}%)")
    print(f"  ─── Capacity hints (for --capacities) ────────────────────────────")
    print(f"  prefix-only capacity  : {prefix_blocks} blocks (protects all sys_prompt)")
    print(f"  10% working set       : ~{unique_blocks//10:,} blocks")
    print(f"  25% working set       : ~{unique_blocks//4:,} blocks")
    print(f"  50% working set       : ~{unique_blocks//2:,} blocks")
    print(f"──────────────────────────────────────────────────────────────────\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate synthetic trace calibrated to Qwen-V3.5-27B-64K",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--output", default="data/trace1_synthetic.csv",
                   help="Output CSV path")
    p.add_argument("--num-requests", type=int, default=2000,
                   help="Total number of requests to generate")
    p.add_argument("--num-users", type=int, default=400,
                   help="Size of the user pool (actual unique users will be fewer)")
    p.add_argument("--prefix-blocks", type=int, default=50,
                   help="Number of shared common-prefix blocks (system prompt)")
    p.add_argument("--prefix-coverage", type=float, default=0.68,
                   help="Fraction of requests that include the common prefix")
    p.add_argument("--session-blocks", type=int, default=40,
                   help="Blocks of shared session content per session (conversation history)")
    p.add_argument("--tail-blocks", type=int, default=5,
                   help="Unique tail blocks per request (new user input, never reused)")
    p.add_argument("--min-turns", type=int, default=2,
                   help="Minimum requests per session")
    p.add_argument("--max-turns", type=int, default=8,
                   help="Maximum requests per session")
    p.add_argument("--reuse-time-p80", type=float, default=46.0,
                   help="F13 p80 reuse time (s) — sets within-session inter-request timing")
    p.add_argument("--time-span", type=float, default=3600.0,
                   help="Total trace time window (s)")
    p.add_argument("--seed", type=int, default=42,
                   help="Random seed for reproducibility")
    p.add_argument("--burst-fraction", type=float, default=0.0,
                   help="Fraction of requests that are cache-pollution bursts "
                        "(one-time, large unique blocks; 0 = no bursts)")
    p.add_argument("--burst-blocks", type=int, default=0,
                   help="Unique blocks per burst request (ignored when --burst-fraction=0)")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress statistics output")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    rows = generate(
        num_requests=args.num_requests,
        num_users=args.num_users,
        prefix_blocks=args.prefix_blocks,
        prefix_coverage=args.prefix_coverage,
        session_blocks=args.session_blocks,
        tail_blocks=args.tail_blocks,
        min_turns=args.min_turns,
        max_turns=args.max_turns,
        reuse_time_p80=args.reuse_time_p80,
        time_span=args.time_span,
        seed=args.seed,
        burst_fraction=args.burst_fraction,
        burst_blocks=args.burst_blocks,
    )

    if not args.quiet:
        print_stats(rows, args.prefix_blocks)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    fieldnames = ["timestamp", "model_id", "user_id", "request_type",
                  "input_length", "hash_ids", "output_length", "turn"]
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Synthetic trace written: {args.output}  ({len(rows):,} requests)")


if __name__ == "__main__":
    main()
