"""Edge labeling over the Neo4j adjacency graph.

Step 2 of the traversability prereq, per
``docs/workstreams/corridor-prereq-2-traversability.md``. Derives one
of three states for every ``ADJACENT_TO`` edge based purely on the
source / destination families and the classification module. No path
search, no inference, no weights.

States:

- ``seed_valid``    — both endpoints in ``DRIVABLE_FAMILIES``. The
                      edge is a candidate member of the traversability
                      subgraph by the Phase 2 allowlist rule.
- ``unsupported``   — at least one endpoint in ``NON_DRIVABLE_FAMILIES``.
                      The edge cannot be drivable regardless of
                      downstream evidence, and is excluded from the
                      subgraph.
- ``unknown``       — either endpoint is ``AMBIGUOUS`` (and the other
                      is not ``NON_DRIVABLE``). Pending per-block-type
                      review in Phase 3. Excluded from the subgraph
                      by default; promotable on evidence.

The ``unsupported`` rule takes precedence over ``unknown``: a
NON_DRIVABLE ∪ AMBIGUOUS edge is still unsupported, because the
NON_DRIVABLE side closes the edge regardless of whether we later
resolve the ambiguous side.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import neo4j

from src.corridor.traversability.classification import (
    CLASSIFICATION_VERSION,
    FamilyBucket,
    classify_family,
)

_LOG = logging.getLogger(__name__)

# State values kept as plain strings (not an Enum) for direct Neo4j
# property storage. The Enum-backed FamilyBucket stays inside Python;
# the persisted graph property reads as a human-legible string.
STATE_SEED_VALID = "seed_valid"
STATE_UNSUPPORTED = "unsupported"
STATE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class EdgeLabel:
    """Computed label for a single edge. ``rule_support`` is True iff
    the edge was seed-admitted by the family allowlist — Phase 3's
    evidence aggregator reads it separately from ``state`` so a
    seed-admitted edge that later accumulates negative evidence still
    carries the seed-support flag in its provenance."""
    state: str
    rule_support: bool


def label_edge(src_family: str, dst_family: str) -> EdgeLabel:
    """Pure labeling function. Family names are looked up against the
    classification module; unseen families resolve to AMBIGUOUS, which
    then combines per the precedence rules above.
    """
    src = classify_family(src_family)
    dst = classify_family(dst_family)
    # Precedence: NON_DRIVABLE on either side closes the edge,
    # regardless of the other side.
    if src is FamilyBucket.NON_DRIVABLE or dst is FamilyBucket.NON_DRIVABLE:
        return EdgeLabel(state=STATE_UNSUPPORTED, rule_support=False)
    if src is FamilyBucket.DRIVABLE and dst is FamilyBucket.DRIVABLE:
        return EdgeLabel(state=STATE_SEED_VALID, rule_support=True)
    return EdgeLabel(state=STATE_UNKNOWN, rule_support=False)


@dataclass
class LabelingStats:
    """Summary counts from a labeling run, for immediate suppression
    validation (Step 3)."""
    started_at: datetime
    edges_seen: int = 0
    seed_valid: int = 0
    unsupported: int = 0
    unknown: int = 0
    completed_at: datetime | None = None

    @property
    def suppression_fraction(self) -> float:
        """Fraction of edges removed from the traversability subgraph
        by this pass. unsupported + unknown both stay out of the
        default subgraph view (only seed_valid is admitted)."""
        if self.edges_seen == 0:
            return 0.0
        return (self.unsupported + self.unknown) / self.edges_seen

    @property
    def unsupported_fraction(self) -> float:
        """Fraction of edges hard-rejected as unsupported. This is the
        number the §8.2 commit bar tests (≥80% deco/support removal),
        though the commit bar is scoped to the 10-map validation set
        rather than the global aggregate graph."""
        if self.edges_seen == 0:
            return 0.0
        return self.unsupported / self.edges_seen

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "edges_seen": self.edges_seen,
            "seed_valid": self.seed_valid,
            "unsupported": self.unsupported,
            "unknown": self.unknown,
            "suppression_fraction": round(self.suppression_fraction, 4),
            "unsupported_fraction": round(self.unsupported_fraction, 4),
        }


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class TraversabilityLabeler:
    """Batch-labels every ``ADJACENT_TO`` edge in the graph.

    The Neo4j adjacency graph is aggregated across maps — one Block
    node per (family, type, variant) identity, one edge per ordered
    identity pair. Labels are therefore GLOBAL: ``(Platform → Deco)``
    is unsupported everywhere, not per-map. Per-map evidence enters
    in Phase 3 via the ``traversability_edge_evidence`` table defined
    in the design note §6.1; Phase 2 stays at the aggregate graph.

    The labeler writes two edge properties:

    - ``traversability_state`` — the :func:`label_edge` state string
    - ``traversability_labeled_at`` — ISO-8601 UTC timestamp of the
      labeling pass, so stale labels are identifiable when the
      classification module is revisited

    Plus a third property carrying the classification version:

    - ``traversability_classification_version`` — mirrors the
      :data:`~src.corridor.traversability.classification.CLASSIFICATION_VERSION`
      at labeling time. Downstream queries can detect edges labeled
      under an older classification and trigger a re-label.
    """

    _FETCH_QUERY = """
    MATCH (a:Block)-[r:ADJACENT_TO]->(b:Block)
    RETURN elementId(r) AS edge_id, a.family AS src_family, b.family AS dst_family
    """

    _UPDATE_QUERY = """
    UNWIND $labels AS lbl
    MATCH ()-[r:ADJACENT_TO]->() WHERE elementId(r) = lbl.edge_id
    SET r.traversability_state = lbl.state,
        r.traversability_rule_support = lbl.rule_support,
        r.traversability_labeled_at = lbl.labeled_at,
        r.traversability_classification_version = lbl.version
    """

    def __init__(self, driver: neo4j.Driver, *, batch_size: int = 2000) -> None:
        self._driver = driver
        self._batch_size = int(batch_size)

    def run(self) -> LabelingStats:
        stats = LabelingStats(started_at=_utcnow())
        labeled_at = stats.started_at.isoformat()
        with self._driver.session() as session:
            edges = [dict(r) for r in session.run(self._FETCH_QUERY)]

        buckets: list[dict[str, Any]] = []
        for e in edges:
            label = label_edge(str(e["src_family"] or ""), str(e["dst_family"] or ""))
            stats.edges_seen += 1
            if label.state == STATE_SEED_VALID:
                stats.seed_valid += 1
            elif label.state == STATE_UNSUPPORTED:
                stats.unsupported += 1
            else:
                stats.unknown += 1
            buckets.append({
                "edge_id": e["edge_id"],
                "state": label.state,
                "rule_support": label.rule_support,
                "labeled_at": labeled_at,
                "version": CLASSIFICATION_VERSION,
            })

        with self._driver.session() as session:
            for i in range(0, len(buckets), self._batch_size):
                chunk = buckets[i:i + self._batch_size]
                session.run(self._UPDATE_QUERY, labels=chunk).consume()

        stats.completed_at = _utcnow()
        return stats
