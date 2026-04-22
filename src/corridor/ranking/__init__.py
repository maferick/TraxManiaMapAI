"""Phase 4: learned corridor ranking model.

Trains a simple ridge-regression model on corridor features and
synthetic inverse-rank labels. Compared head-to-head against the
``corridor_confidence`` heuristic on the PR-7 proxy cohorts.

No DB persistence at this phase — the model JSON + training report
are the only outputs. If the learned model meaningfully beats the
heuristic, a follow-up PR wires persistence + an evaluator. If not,
the heuristic stays canonical and this phase is a documented
negative result.

See ``docs/workstreams/corridor-inference.md`` for the parent
charter and ``scoring.py`` for the heuristic it's compared against.
"""
from src.corridor.ranking.features import (
    CorridorFeatureVector,
    CorridorRow,
    FEATURE_NAMES,
    build_feature_matrix,
    load_corridor_rows,
)
from src.corridor.ranking.labels import synthesize_inverse_rank_labels
from src.corridor.ranking.model import RidgeRegression, TrainingReport
from src.corridor.ranking.train import train_and_evaluate

__all__ = [
    "CorridorFeatureVector",
    "CorridorRow",
    "FEATURE_NAMES",
    "RidgeRegression",
    "TrainingReport",
    "build_feature_matrix",
    "load_corridor_rows",
    "synthesize_inverse_rank_labels",
    "train_and_evaluate",
]
