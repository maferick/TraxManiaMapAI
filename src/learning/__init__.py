"""Learning-layer analytical tools.

Anything that interprets or compares outputs of the ranking pipelines
without being a ranking pipeline itself (e.g. A/B snapshot comparison,
future metrics consolidation).
"""
from src.learning.compare_snapshots import (
    SnapshotComparison,
    SnapshotSummary,
    build_comparison,
    render_markdown,
)

__all__ = [
    "SnapshotComparison",
    "SnapshotSummary",
    "build_comparison",
    "render_markdown",
]
