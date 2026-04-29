"""Offline block registry — identifies high-value prefix blocks.

Registry file format (TSV, lines starting with '#' are comments)
-----------------------------------------------------------------
    # block_hash  hit_count  description
    a3f2c8...     1842       top-1 system prompt prefix chain
    b7d1e9...     1205       top-2 system prompt prefix chain

Two registries are supported:
  hard_protected  — blocks that bypass Probation entirely and go directly
                    to Hard Protected (future; treated as Protected in MVP)
  protected       — blocks that enter Protected on first insertion

OfflineRegistry exposes O(1) lookup via frozensets.
"""
from __future__ import annotations

from typing import Optional, Set


class OfflineRegistry:
    """In-memory lookup table loaded from offline-generated registry files.

    Usage
    -----
    registry = OfflineRegistry.from_files(
        protected_path="data/protected_registry.txt",
        hard_protected_path="data/hard_protected_registry.txt",   # optional
    )
    registry.is_protected("a3f2c8...")   # True / False
    """

    def __init__(
        self,
        protected: Set[str],
        hard_protected: Optional[Set[str]] = None,
    ) -> None:
        self._protected: frozenset[str] = frozenset(protected)
        self._hard_protected: frozenset[str] = frozenset(hard_protected or set())

    # ------------------------------------------------------------------
    # Lookup API
    # ------------------------------------------------------------------

    def is_protected(self, block_hash: str) -> bool:
        """True if block should enter Protected queue on first insertion."""
        return block_hash in self._protected or block_hash in self._hard_protected

    def is_hard_protected(self, block_hash: str) -> bool:
        """True if block is in the Hard Protected tier (reserved for Phase 2)."""
        return block_hash in self._hard_protected

    def __len__(self) -> int:
        return len(self._protected) + len(self._hard_protected)

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_files(
        cls,
        protected_path: Optional[str] = None,
        hard_protected_path: Optional[str] = None,
    ) -> "OfflineRegistry":
        """Load one or both registry files from disk."""
        protected = _load_registry_file(protected_path) if protected_path else set()
        hard_protected = (
            _load_registry_file(hard_protected_path) if hard_protected_path else set()
        )
        return cls(protected=protected, hard_protected=hard_protected)

    @classmethod
    def empty(cls) -> "OfflineRegistry":
        return cls(protected=set())


def _load_registry_file(path: str) -> Set[str]:
    hashes: Set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # First whitespace-separated token is the block hash
            block_hash = line.split()[0]
            hashes.add(block_hash)
    return hashes
