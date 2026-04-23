"""Learning-layer analytical tools.

Anything that interprets or compares outputs of the ranking pipelines
without being a ranking pipeline itself (e.g. A/B snapshot comparison,
scores, metrics history).
"""
from src.learning.compare_snapshots import (
    SnapshotComparison,
    SnapshotSummary,
    build_comparison,
    render_markdown,
)
from src.learning.metrics_persistence import (
    MetricInsert,
    MetricRow,
    history_for_scheme,
    latest_per_scheme,
    new_run_id,
    record_many,
)
from src.learning.scores import (
    QualityInputs,
    ReadinessReport,
    TrendSample,
    ai_quality_score,
    generation_readiness,
    trend_direction,
    variety_score,
)

__all__ = [
    "MetricInsert",
    "MetricRow",
    "QualityInputs",
    "ReadinessReport",
    "SnapshotComparison",
    "SnapshotSummary",
    "TrendSample",
    "ai_quality_score",
    "build_comparison",
    "generation_readiness",
    "history_for_scheme",
    "latest_per_scheme",
    "new_run_id",
    "record_many",
    "render_markdown",
    "trend_direction",
    "variety_score",
]
