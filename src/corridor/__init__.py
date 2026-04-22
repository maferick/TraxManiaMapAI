"""Corridor-inference subsystem.

Derives a movement-feasible subgraph from the raw adjacency graph
(Neo4j `ADJACENT_TO` edges), constrained by checkpoint anchors, so
downstream code can reason about checkpoint-to-checkpoint corridor
candidates without needing position telemetry.

Design contract: ``docs/workstreams/corridor-prereq-2-traversability.md``.

Phase 1 artifact: the family classification lives in
``src/corridor/traversability/classification.py`` — hard buckets, no
heuristics, no scoring.
"""
from src.corridor.corridor_scoring_pipeline import (
    ScoringStats,
    score_corridors,
    score_map_corridors,
)
from src.corridor.scoring import (
    SCORE_VERSION,
    EdgeEvidence,
    score_corridor,
    score_edge,
)

__all__ = [
    "EdgeEvidence",
    "SCORE_VERSION",
    "ScoringStats",
    "score_corridor",
    "score_corridors",
    "score_edge",
    "score_map_corridors",
]
