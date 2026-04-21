"""Concrete evaluators, PR 7 scaffold.

All evaluators are opt-in registered. Dependencies (DB connections,
Neo4j drivers, parser versions) are injected at construction time;
the :meth:`evaluate` method only receives ``map_id`` and optional
``benchmark_set_version``.

Scaffold-quality: the heuristics these three evaluators use are
placeholders built to demonstrate the wiring end-to-end. Real tuning
lands after real data passes through PRs 3–6.
"""
from src.evaluation.evaluators.adjacency import AdjacencyGraphEvaluator
from src.evaluation.evaluators.route_coverage import RouteCoverageEvaluator
from src.evaluation.evaluators.structural import StructuralEvaluator

__all__ = [
    "AdjacencyGraphEvaluator",
    "RouteCoverageEvaluator",
    "StructuralEvaluator",
]
