"""Versioned evaluators. See docs/evaluation-plan.md and docs/architecture.md."""
from .base import Evaluator, EvaluationResult, utcnow
from .evaluators import (
    AdjacencyGraphEvaluator,
    BehaviorProfileEvaluator,
    CorridorConfidenceEvaluator,
    RouteCoverageEvaluator,
    StructuralEvaluator,
)
from .registry import all_registered, get, register
from .versioning import EvaluatorVersion, VersionCompatibility, invalidates_rankings

__all__ = [
    "AdjacencyGraphEvaluator",
    "BehaviorProfileEvaluator",
    "CorridorConfidenceEvaluator",
    "Evaluator",
    "EvaluationResult",
    "EvaluatorVersion",
    "RouteCoverageEvaluator",
    "StructuralEvaluator",
    "VersionCompatibility",
    "all_registered",
    "get",
    "invalidates_rankings",
    "register",
    "utcnow",
]
