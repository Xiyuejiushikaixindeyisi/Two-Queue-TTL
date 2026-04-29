"""Policy registry — maps string names to policy classes.

To add a new policy:
  1. Create sim/policies/<name>.py and subclass AbstractCachePolicy.
  2. Add an entry here.
  3. Done — SimulationRunner picks it up automatically via SimConfig.policy.

Strategy matrix (experiment plan §5.2)
---------------------------------------
  S0  infinite_cache  — No eviction; ideal_hit_rate upper bound
  S1  lru             — Production baseline
  S2  belady          — Optimal offline oracle; algorithmic upper bound
  S3  ttl_lru         — LRU + TTL expiry (isolates TTL contribution)
  S5  two_queue_ttl   — Probation/Protected queues + F13-anchored TTL
"""
from .belady import BeladyOraclePolicy
from .infinite_cache import InfiniteCachePolicy
from .lru import LRUPolicy
from .ttl_lru import TTLLRUPolicy
from .two_queue_ttl import TwoQueueTTLPolicy

POLICY_REGISTRY = {
    "infinite_cache": InfiniteCachePolicy,   # S0
    "lru": LRUPolicy,                        # S1
    "belady": BeladyOraclePolicy,            # S2 — needs future_index; see SimulationRunner
    "ttl_lru": TTLLRUPolicy,                 # S3
    "two_queue_ttl": TwoQueueTTLPolicy,      # S5/S6/S7
}

__all__ = [
    "POLICY_REGISTRY",
    "BeladyOraclePolicy",
    "InfiniteCachePolicy",
    "LRUPolicy",
    "TTLLRUPolicy",
    "TwoQueueTTLPolicy",
]
