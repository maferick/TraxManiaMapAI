"""Constraint-graph domain types.

``BlockKey`` is the in-Python identity of a graph node; its
``normalized_key`` is the string stored under ``:Block.key`` in
Neo4j. ``AdjacencyObservation`` is the single-map extraction output;
``AdjacencyEdge`` is the accumulated shape we read back from Neo4j.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


_SEP = "|"


@dataclass(frozen=True, order=True)
class BlockKey:
    family: str
    type: str
    variant: str = ""

    def __post_init__(self) -> None:
        for name, value in (("family", self.family), ("type", self.type)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if _SEP in self.family or _SEP in self.type or _SEP in self.variant:
            raise ValueError(
                f"key components must not contain the separator {_SEP!r}"
            )

    @property
    def normalized_key(self) -> str:
        return f"{self.family}{_SEP}{self.type}{_SEP}{self.variant}"

    @classmethod
    def from_normalized(cls, key: str) -> BlockKey:
        parts = key.split(_SEP)
        if len(parts) != 3:
            raise ValueError(f"expected 3 parts in normalized key {key!r}")
        return cls(family=parts[0], type=parts[1], variant=parts[2])


def order_pair(a: BlockKey, b: BlockKey) -> tuple[BlockKey, BlockKey]:
    """Deterministic lexicographic ordering so undirected adjacency is stored once."""
    return (a, b) if a.normalized_key <= b.normalized_key else (b, a)


@dataclass(frozen=True)
class AdjacencyObservation:
    """One observation of an adjacency within a single map.

    The pipeline aggregates these (per-map, deduped) into Neo4j edge
    updates. Observations are always stored in normalized order
    (``a <= b`` lexicographically); direction information is intentionally
    not carried — a directed ``:TRANSITION`` edge with replay evidence
    is future work.
    """

    a: BlockKey
    b: BlockKey
    snapshot_id: str
    is_benchmark_strong: bool = False
    is_broken_fixture: bool = False

    def __post_init__(self) -> None:
        if self.a.normalized_key > self.b.normalized_key:
            raise ValueError(
                "AdjacencyObservation requires lexicographic pair order; "
                "use order_pair() on construction"
            )


@dataclass(frozen=True)
class AdjacencyEdge:
    """Accumulated edge shape as read back from Neo4j.

    Evidence fields are counts across distinct maps, not raw sample
    counts. ``validity_label`` is derived — never stored independently
    from the other fields.
    """

    a: BlockKey
    b: BlockKey
    observed_in_maps_count: int
    benchmark_strong_count: int
    broken_fixture_count: int
    replay_supported_count: int
    validity_label: str
    first_seen_snapshot: str | None = None
    last_seen_snapshot: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
