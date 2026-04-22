"""Corridor diversity metrics (A3).

Measures whether learned ranking is collapsing route variety. Pure
similarity helpers + metrics; no DB dependency inside the primitives
so tests run without MariaDB.

See docs/learning/corridor-diversity-metrics.md.
"""
from src.diversity.metrics import (
    CorridorPath,
    DiversityReport,
    IntervalDiversity,
    RankerDiversitySummary,
    build_report,
    compute_interval_diversity,
    fetch_paths,
    jaccard,
    top_k_pairwise_mean_similarity,
)

__all__ = [
    "CorridorPath",
    "DiversityReport",
    "IntervalDiversity",
    "RankerDiversitySummary",
    "build_report",
    "compute_interval_diversity",
    "fetch_paths",
    "jaccard",
    "top_k_pairwise_mean_similarity",
]
