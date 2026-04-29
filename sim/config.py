from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class TTLLRUConfig:
    """Parameters for the TTL-LRU eviction policy."""

    ttl: float = 3600.0


@dataclass
class TwoQueueTTLConfig:
    """Parameters for the Two-Queue TTL eviction policy.

    TTL anchor points are derived from F13 reuse_time data:
      base_ttl     → F13 p80  = 46 s  (protects 80% of reuse events)
      extended_ttl → F13 p95  = 255 s (protects high-frequency blocks)
    """

    base_ttl: float = 46.0
    extended_ttl: float = 255.0
    promotion_threshold: int = 2    # hit_count required to promote Probation → Protected
    protected_ratio: float = 0.30   # fraction of total capacity reserved for Protected queue


@dataclass
class SimConfig:
    """Top-level configuration for a single simulation run.

    Attributes
    ----------
    cache_capacity:
        Total number of blocks the simulated cache can hold.
    block_size:
        Tokens per block (used to convert block counts to token counts).
    policy:
        Key into POLICY_REGISTRY selecting the eviction strategy.
    ttl_lru:
        Parameters used when policy == "ttl_lru".
    two_queue_ttl:
        Parameters used when policy == "two_queue_ttl".
    registry_path:
        Optional path to an offline block registry file.
        When provided, matching blocks enter Protected directly.
    """

    cache_capacity: int
    block_size: int = 128
    policy: str = "lru"
    ttl_lru: TTLLRUConfig = field(default_factory=TTLLRUConfig)
    two_queue_ttl: TwoQueueTTLConfig = field(default_factory=TwoQueueTTLConfig)
    registry_path: Optional[str] = None


@dataclass
class PolicyConfig:
    """Per-policy config entry used in ExperimentConfig.policies list."""

    policy: str = "lru"
    capacity: int = 1000
    block_size: int = 128
    ttl_lru: TTLLRUConfig = field(default_factory=TTLLRUConfig)
    two_queue_ttl: TwoQueueTTLConfig = field(default_factory=TwoQueueTTLConfig)

    def to_sim_config(self) -> SimConfig:
        return SimConfig(
            cache_capacity=self.capacity,
            block_size=self.block_size,
            policy=self.policy,
            ttl_lru=self.ttl_lru,
            two_queue_ttl=self.two_queue_ttl,
        )


@dataclass
class ExperimentConfig:
    """Configuration for a multi-policy comparison experiment."""

    name: str
    trace_path: str
    output_dir: str
    policies: List[PolicyConfig] = field(default_factory=list)
    description: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> ExperimentConfig:
        """Deserialize from a plain dict (e.g. loaded from JSON)."""
        policies = []
        for p in d.get("policies", []):
            ttl_lru_cfg = TTLLRUConfig(**p.get("ttl_lru", {}))
            two_queue_ttl_cfg = TwoQueueTTLConfig(**p.get("two_queue_ttl", {}))
            policies.append(PolicyConfig(
                policy=p.get("policy", "lru"),
                capacity=p.get("capacity", 1000),
                block_size=p.get("block_size", 128),
                ttl_lru=ttl_lru_cfg,
                two_queue_ttl=two_queue_ttl_cfg,
            ))
        return cls(
            name=d.get("name", ""),
            trace_path=d.get("trace_path", ""),
            output_dir=d.get("output_dir", "outputs"),
            policies=policies,
            description=d.get("description", ""),
        )
