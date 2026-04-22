"""Diagnostics for corridor-ranking score compression.

Background: the Phase 4 v0.3 dry-run showed the learned
corridor-score distribution was ~40% as spread as the heuristic
corridor_confidence distribution (stdev 0.068 vs 0.173). That might
be:

- a **label** artifact — the time_envelope labels themselves are
  narrow, so the model has no range to learn
- a **regularization** artifact — ridge α=1.0 shrinks weights toward
  zero, flattening predictions
- a **feature** artifact — the features available simply can't
  distinguish corridors more sharply than what the model shows

These three causes have different signatures. This module surfaces
the data needed to distinguish them:

- ``label_distribution_summary`` — quartiles + stdev of labels. If a
  label scheme's stdev is already narrow, that alone explains the
  compressed predictions.
- ``regularization_sweep`` — train the same feature matrix + labels
  at a range of alphas; report prediction stdev at each. If stdev
  rises sharply as α→0, the compression is regularization-bound.
- ``feature_ablation`` — zero out one feature at a time, retrain,
  report the prediction-stdev delta. Reveals which features carry
  the variance (and which contribute nothing).

All three are READ-ONLY diagnostics — they don't mutate DB state or
the production model. The CLI just runs them and prints a report.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from src.corridor.ranking.features import CorridorFeatureVector
from src.corridor.ranking.model import (
    RidgeRegression,
    auc_roc,
    rmse,
    spearman_rank_corr,
)


@dataclass(frozen=True)
class LabelSpreadSummary:
    """Quartile + stdev snapshot of a label set. All numeric — keep
    interpretation to the reader (nothing here is a pass/fail gate).
    """
    label_scheme: str
    count: int
    minimum: float
    q1: float
    median: float
    q3: float
    maximum: float
    mean: float
    stdev: float


def label_distribution_summary(
    label_scheme: str, label_by_id: dict[int, float]
) -> LabelSpreadSummary | None:
    """Return quartile + stdev summary of the label values, or None
    when there are no labels.

    This surfaces the label's intrinsic spread. If the learned-score
    stdev is similar to the label stdev, the model is reproducing
    what the label gives it — compression comes from the label, not
    from regularization.
    """
    if not label_by_id:
        return None
    arr = np.array(list(label_by_id.values()), dtype=np.float64)
    return LabelSpreadSummary(
        label_scheme=label_scheme,
        count=int(arr.size),
        minimum=float(arr.min()),
        q1=float(np.quantile(arr, 0.25)),
        median=float(np.median(arr)),
        q3=float(np.quantile(arr, 0.75)),
        maximum=float(arr.max()),
        mean=float(arr.mean()),
        stdev=float(arr.std(ddof=1)) if arr.size >= 2 else 0.0,
    )


@dataclass(frozen=True)
class RegularizationSweepRow:
    """One α point on the alpha sweep."""
    alpha: float
    train_rmse: float
    test_rmse: float
    test_rank_corr: float
    pred_stdev_all: float            # stdev of model.predict on the FULL feature matrix
    pred_range_all: float            # max - min on the FULL feature matrix
    weight_l2_norm: float            # sqrt of sum of squared weights — shows how hard ridge is shrinking
    auc_learned: float | None        # proxy-cohort AUC if pos/neg sets provided
    n_auc_maps: int                  # number of cohort-matched maps the AUC was computed from


def _deterministic_split(
    n: int, test_frac: float, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    """Same deterministic shuffle as train._deterministic_split. Kept
    local so diagnostics stay independent of train.py internals."""
    rng = np.random.default_rng(seed)
    idx = np.arange(n)
    rng.shuffle(idx)
    cut = int(n * (1 - test_frac))
    return idx[:cut], idx[cut:]


def _per_map_mean(
    vectors: Sequence[CorridorFeatureVector], scores: np.ndarray
) -> dict[int, float]:
    by_map: dict[int, list[float]] = {}
    for vec, score in zip(vectors, scores):
        by_map.setdefault(vec.map_id, []).append(float(score))
    return {m: (sum(s) / len(s)) for m, s in by_map.items() if s}


def _cohort_auc(
    per_map_score: dict[int, float],
    pos_ids: set[int],
    neg_ids: set[int],
) -> tuple[float | None, int]:
    pos = [per_map_score[m] for m in pos_ids if m in per_map_score]
    neg = [per_map_score[m] for m in neg_ids if m in per_map_score]
    n = len(pos) + len(neg)
    if not pos or not neg:
        return None, n
    scores = np.array(pos + neg, dtype=np.float64)
    labels = np.array([1] * len(pos) + [0] * len(neg), dtype=np.int64)
    return auc_roc(scores, labels), n


def regularization_sweep(
    *,
    vectors: Sequence[CorridorFeatureVector],
    X: np.ndarray,
    y: np.ndarray,
    alphas: Sequence[float],
    feature_names: tuple[str, ...],
    pos_ids: set[int] | None = None,
    neg_ids: set[int] | None = None,
    test_frac: float = 0.2,
    random_seed: int = 42,
) -> list[RegularizationSweepRow]:
    """Train the same data at each alpha, report metrics + prediction
    stdev. The train/test split is shared across all alphas (same
    seed) so the RMSE/rank-corr columns are comparable across rows.

    ``pred_stdev_all`` is the stdev of predictions on the *full*
    feature matrix X — that's the quantity dry-run cares about
    (learned_corridor_score distribution). ``weight_l2_norm`` shows
    how hard ridge is shrinking — monotone-decreasing with α as a
    sanity check.
    """
    train_idx, test_idx = _deterministic_split(
        n=X.shape[0], test_frac=test_frac, seed=random_seed,
    )
    X_train, X_test = X[train_idx], X[test_idx]
    y_train, y_test = y[train_idx], y[test_idx]

    rows: list[RegularizationSweepRow] = []
    for alpha in alphas:
        model = RidgeRegression(alpha=alpha, feature_names=feature_names)
        model.fit(X_train, y_train)
        assert model.weights is not None
        pred_train = model.predict(X_train)
        pred_test = model.predict(X_test)
        pred_all = model.predict(X)
        per_map = _per_map_mean(vectors, pred_all)
        auc, n_auc = (
            _cohort_auc(per_map, pos_ids, neg_ids)
            if (pos_ids and neg_ids) else (None, 0)
        )
        rows.append(RegularizationSweepRow(
            alpha=float(alpha),
            train_rmse=rmse(pred_train, y_train),
            test_rmse=rmse(pred_test, y_test),
            test_rank_corr=spearman_rank_corr(pred_test, y_test),
            pred_stdev_all=float(pred_all.std(ddof=1)) if pred_all.size >= 2 else 0.0,
            pred_range_all=float(pred_all.max() - pred_all.min()) if pred_all.size else 0.0,
            weight_l2_norm=float(np.linalg.norm(model.weights)),
            auc_learned=auc,
            n_auc_maps=n_auc,
        ))
    return rows


@dataclass(frozen=True)
class FeatureAblationRow:
    """One row = "what happens when this feature is zeroed out during
    training" relative to the full model."""
    feature_name: str
    pred_stdev_all: float
    pred_stdev_delta: float          # ablated_stdev - full_stdev (negative → feature CARRIES variance)
    test_rank_corr: float
    test_rank_corr_delta: float
    auc_learned: float | None
    auc_delta: float | None


def feature_ablation(
    *,
    vectors: Sequence[CorridorFeatureVector],
    X: np.ndarray,
    y: np.ndarray,
    alpha: float,
    feature_names: tuple[str, ...],
    pos_ids: set[int] | None = None,
    neg_ids: set[int] | None = None,
    test_frac: float = 0.2,
    random_seed: int = 42,
) -> tuple[RegularizationSweepRow, list[FeatureAblationRow]]:
    """Return (baseline_row, per_feature_rows).

    Baseline = full model at the given α. Per-feature rows = train the
    same model with that feature's column zeroed. The *delta* columns
    show how much predictive spread / rank correlation / AUC each
    feature carries. Large negative pred_stdev_delta = feature
    contributes materially to variance; ~0 = feature is dead weight
    at this α.

    Zeroing the column (vs. removing it) keeps the feature-matrix
    shape + weight index stable, so the test/train split and all
    other training discipline stay exactly the same — cleaner
    comparison than dropping + re-indexing.
    """
    if X.shape[1] != len(feature_names):
        raise ValueError(
            f"X has {X.shape[1]} cols but feature_names has "
            f"{len(feature_names)} entries",
        )
    baseline_rows = regularization_sweep(
        vectors=vectors, X=X, y=y, alphas=[alpha],
        feature_names=feature_names,
        pos_ids=pos_ids, neg_ids=neg_ids,
        test_frac=test_frac, random_seed=random_seed,
    )
    baseline = baseline_rows[0]

    rows: list[FeatureAblationRow] = []
    for i, name in enumerate(feature_names):
        X_ab = X.copy()
        X_ab[:, i] = 0.0
        ab_rows = regularization_sweep(
            vectors=vectors, X=X_ab, y=y, alphas=[alpha],
            feature_names=feature_names,
            pos_ids=pos_ids, neg_ids=neg_ids,
            test_frac=test_frac, random_seed=random_seed,
        )
        ab = ab_rows[0]
        rows.append(FeatureAblationRow(
            feature_name=name,
            pred_stdev_all=ab.pred_stdev_all,
            pred_stdev_delta=ab.pred_stdev_all - baseline.pred_stdev_all,
            test_rank_corr=ab.test_rank_corr,
            test_rank_corr_delta=ab.test_rank_corr - baseline.test_rank_corr,
            auc_learned=ab.auc_learned,
            auc_delta=(
                ab.auc_learned - baseline.auc_learned
                if (ab.auc_learned is not None and baseline.auc_learned is not None)
                else None
            ),
        ))
    return baseline, rows
