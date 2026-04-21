"""Canonical entity dataclasses. One-to-one with MariaDB tables under
``migrations/mariadb/``. See ``docs/data-contracts.md`` for the full
provenance contract.

``EvaluationResult`` (the shape of an evaluation artifact) lives in
``src.evaluation.base`` because it belongs to the evaluator subsystem.
"""
from .maps import BlockPlacement, Map
from .provenance import IngestionSnapshot, StageRun, StageStatus
from .replays import CleanStatus, Replay, ReplayCohort, ReplayFeatures
from .routes import RouteArtifact

__all__ = [
    "BlockPlacement",
    "CleanStatus",
    "IngestionSnapshot",
    "Map",
    "Replay",
    "ReplayCohort",
    "ReplayFeatures",
    "RouteArtifact",
    "StageRun",
    "StageStatus",
]
