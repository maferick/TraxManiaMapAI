"""Versioned evaluators. See docs/evaluation-plan.md and docs/architecture.md."""
from .base import Evaluator, EvaluationResult, utcnow
from .registry import all_registered, get, register
from .versioning import EvaluatorVersion, VersionCompatibility, invalidates_rankings

__all__ = [
    "Evaluator",
    "EvaluationResult",
    "EvaluatorVersion",
    "VersionCompatibility",
    "all_registered",
    "get",
    "invalidates_rankings",
    "register",
    "utcnow",
]
