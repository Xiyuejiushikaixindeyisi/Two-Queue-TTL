"""Tests for lib/chains.py (canonical trie + chain-forest algorithm)."""
from __future__ import annotations

from lib.chains import (
    TrieNode,
    build_trie,
    count_trie_hits,
    find_chain_forest,
    trie_insert,
)


def test_trie_insert_counts():
    root = TrieNode()
    trie_insert(root, [b"a", b"b"], "r1")
    trie_insert(root, [b"a", b"c"], "r2")
    assert root.count == 2
    assert root.children[b"a"].count == 2
    assert root.children[b"a"].children[b"b"].count == 1


def test_count_trie_hits_equals_total_minus_unique():
    # 3 identical 2-block requests: total 6 blocks, 2 unique → 4 hits
    root = TrieNode()
    for i in range(3):
        trie_insert(root, [b"x", b"y"], f"r{i}")
    assert count_trie_hits(root) == 4


def test_build_trie_from_records():
    records = [
        {"request_id": "1", "hash_ids": {"base": [b"x", b"y"]}},
        {"request_id": "2", "hash_ids": {"base": [b"x", b"y"]}},
        {"request_id": "3", "hash_ids": {"base": []}},        # skipped
    ]
    root, n_req, n_blocks = build_trie(records, "base")
    assert n_req == 2 and n_blocks == 4
    assert count_trie_hits(root) == 2


def test_find_chain_forest_detects_shared_prefix():
    root = TrieNode()
    shared = [b"p1", b"p2", b"p3", b"p4"]
    for i in range(3):
        trie_insert(root, shared + [f"x{i}".encode()], f"r{i}")
    forest = find_chain_forest(root, min_chain_length=2, min_chain_coverage=0.0)
    assert forest["chains"]
    top = max(forest["chains"], key=lambda c: c["coverage_count"])
    assert top["chain_length"] >= 4


def test_find_chain_forest_empty_trie():
    forest = find_chain_forest(TrieNode())
    assert forest["chains"] == []
    assert forest["stats"]["trie_total_requests"] == 0
