"""Replay-coverage analysis (A1).

Scores maps by expected value-to-learning from one more replay.
Used by the ``report-replay-coverage`` CLI to emit a backfill
recommendation. Pure Python + SQL — no external services.

See docs/learning/replay-coverage-expansion-plan.md.
"""
from src.coverage.replay_value import (
    SATURATION_PER_MAP,
    CohortThresholdConfig,
    CoverageReport,
    MapCoverage,
    RecommendedBackfill,
    fetch_coverage,
    marginal_gain,
    score_map,
    select_backfill,
)

__all__ = [
    "SATURATION_PER_MAP",
    "CohortThresholdConfig",
    "CoverageReport",
    "MapCoverage",
    "RecommendedBackfill",
    "fetch_coverage",
    "marginal_gain",
    "score_map",
    "select_backfill",
]
