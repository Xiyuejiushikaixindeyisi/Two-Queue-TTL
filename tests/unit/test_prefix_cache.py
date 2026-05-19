"""Unit tests for PrefixCache simulation engine."""

from sim.cache.prefix_cache import PrefixCache
from sim.core.prefix_key import make_prefix_path_keys
from sim.core.trace import TraceRecord
from sim.policies.lru import LRUPolicy


def make_record(
    timestamp: float,
    hash_ids,
    user_id: str = "u1",
    input_length: int = 256,
) -> TraceRecord:
    return TraceRecord(
        timestamp=timestamp,
        model_id="test-model",
        user_id=user_id,
        request_type="chat",
        input_length=input_length,
        hash_ids=hash_ids,
    )


class TestPrefixSemantics:
    def test_cold_cache_all_miss(self):
        cache = PrefixCache(LRUPolicy(capacity=10))
        r = cache.process_request(make_record(1.0, ["h1", "h2", "h3"]))
        assert r.hit_count == 0
        assert r.miss_count == 3

    def test_full_prefix_hit_second_request(self):
        cache = PrefixCache(LRUPolicy(capacity=10))
        cache.process_request(make_record(1.0, ["h1", "h2", "h3"]))
        r = cache.process_request(make_record(2.0, ["h1", "h2", "h3"]))
        assert r.hit_count == 3
        assert r.miss_count == 0

    def test_partial_prefix_hit(self):
        cache = PrefixCache(LRUPolicy(capacity=10))
        cache.process_request(make_record(1.0, ["h1", "h2"]))
        # Second request shares prefix h1,h2 but adds h3,h4
        r = cache.process_request(make_record(2.0, ["h1", "h2", "h3", "h4"]))
        assert r.hit_count == 2
        assert r.miss_count == 2

    def test_prefix_chain_broken_on_first_miss(self):
        """If h1 hits but h2 misses, h3 should also be a miss (prefix semantics)."""
        cache = PrefixCache(LRUPolicy(capacity=10))
        # Insert only h1 manually (simulate h2 was evicted)
        cache.process_request(make_record(1.0, ["h1"]))  # h1 in cache now
        r = cache.process_request(make_record(2.0, ["h1", "h2", "h3"]))
        assert r.hit_count == 1
        assert r.miss_count == 2


class TestEvictionDuringRequest:
    def test_eviction_triggered_when_full(self):
        cache = PrefixCache(LRUPolicy(capacity=2))
        # Fill cache to capacity
        cache.process_request(make_record(1.0, ["h1", "h2"]))
        # Next request needs 2 new blocks but capacity=2 → must evict first
        r = cache.process_request(make_record(2.0, ["h3", "h4"]))
        assert r.eviction_count == 2
        assert cache.policy.size == 2

    def test_pinning_prevents_self_eviction(self):
        """Blocks allocated for the current request must not evict each other."""
        cache = PrefixCache(LRUPolicy(capacity=2))
        # Fill with h1, h2
        cache.process_request(make_record(1.0, ["h1", "h2"]))
        # Now request with 2 new blocks: h1 and h2 should be evicted but
        # h3 and h4 should not evict each other mid-allocation
        cache.process_request(make_record(2.0, ["h3", "h4"]))
        h3_key, h4_key = [k.hex() for k in make_prefix_path_keys("test-model", ["h3", "h4"])]
        assert cache.policy.contains(h3_key)
        assert cache.policy.contains(h4_key)

    def test_hit_blocks_not_evicted_for_same_request(self):
        """Hit blocks (active in this request) must not be evicted to make room
        for the same request's miss blocks."""
        cache = PrefixCache(LRUPolicy(capacity=3))
        cache.process_request(make_record(1.0, ["h1", "h2", "h3"]))
        # h4, h5 fill capacity to 5 > 3 → evictions needed
        # But h1 was a hit for this request so should survive
        r = cache.process_request(make_record(2.0, ["h1", "h2", "h4", "h5"]))
        assert r.hit_count == 2
        h1_key = make_prefix_path_keys("test-model", ["h1"])[0].hex()
        assert cache.policy.contains(h1_key)  # h1 was hit → must still be in cache


class TestRequestResult:
    def test_result_fields(self):
        cache = PrefixCache(LRUPolicy(capacity=10))
        r = cache.process_request(make_record(1.0, ["h1", "h2"], input_length=256))
        assert r.hit_count == 0
        assert r.miss_count == 2
        assert r.eviction_count == 0
        assert r.record.input_length == 256
