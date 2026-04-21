"""Evaluator dry-run (PR 7).

``runner.py`` orchestrates the pass; ``stats.py`` computes
distributional metrics; ``report.py`` renders markdown.
"""
from src.evaluation.dryrun.report import render_markdown
from src.evaluation.dryrun.runner import (
    BenchmarkMembership,
    DryRunMap,
    DryRunReport,
    DryRunRunner,
)
from src.evaluation.dryrun.stats import (
    HistogramBuckets,
    Quartiles,
    disagreement_pairs,
    histogram,
    quartiles,
    separation_auc,
)

__all__ = [
    "BenchmarkMembership",
    "DryRunMap",
    "DryRunReport",
    "DryRunRunner",
    "HistogramBuckets",
    "Quartiles",
    "disagreement_pairs",
    "histogram",
    "quartiles",
    "render_markdown",
    "separation_auc",
]
