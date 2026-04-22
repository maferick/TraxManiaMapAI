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
