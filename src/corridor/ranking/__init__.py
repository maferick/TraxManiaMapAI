"""Phase 4: learned corridor ranking model.

Trains ridge-regression corridor-ranking models on two honest-about-
their-weakness label schemes:

- v0.1 ``inverse_rank`` — synthetic (rank-within-interval proxy)
- v0.2 ``time_envelope`` — weak observed label (does the corridor's
  length fit the elapsed time an actual driver took?)

Both are compared head-to-head against the ``corridor_confidence``
heuristic on the PR-7 proxy cohorts. No DB persistence at this phase —
the model JSON + training report are the only outputs. If the learned
model meaningfully beats the heuristic on the stronger label, a
follow-up PR wires persistence + an evaluator.

See ``docs/workstreams/corridor-inference.md`` for the parent charter
and ``scoring.py`` for the heuristic it's compared against.
"""
from src.corridor.ranking.features import (
    CorridorFeatureVector,
    CorridorRow,
    FEATURE_NAMES,
    build_feature_matrix,
    load_corridor_rows,
)
from src.corridor.ranking.labels import synthesize_inverse_rank_labels
from src.corridor.ranking.model import (
    ComparativeTrainingReport,
    RidgeRegression,
    TrainingReport,
)
from src.corridor.ranking.time_envelope_labels import (
    plausibility,
    synthesize_time_envelope_labels,
)
from src.corridor.ranking.train import train_and_evaluate

__all__ = [
    "ComparativeTrainingReport",
    "CorridorFeatureVector",
    "CorridorRow",
    "FEATURE_NAMES",
    "RidgeRegression",
    "TrainingReport",
    "build_feature_matrix",
    "load_corridor_rows",
    "plausibility",
    "synthesize_inverse_rank_labels",
    "synthesize_time_envelope_labels",
    "train_and_evaluate",
]
