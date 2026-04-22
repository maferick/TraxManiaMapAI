"""Ridge regression for corridor ranking.

Closed-form. No sklearn — numpy is already a project dependency and
the problem is small enough (900 × 11 feature matrix) that the
closed-form (X'X + λI)^-1 X'y solution is instant.

Why ridge (not plain OLS):

- Features overlap: ``mean_path_support_log`` and
  ``max_path_support_log`` are correlated; ``mean_neg_evidence_frac``
  and ``max_neg_evidence_frac`` are correlated. Plain OLS will
  produce unstable weights on correlated features. Ridge
  regularization dampens that.
- Small-sample stability: 900 rows is small; regularization guards
  against overfitting.
- Interpretable weights: learned coefficients per FEATURE_NAMES
  entry stay readable for sanity checking.

``predict`` returns the raw linear score; clip to [0, 1] happens at
the evaluator side if needed for comparability with corridor_confidence.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class RidgeRegression:
    """Closed-form ridge regression. ``feature_names`` kept alongside
    weights for serialization so the output JSON stays self-describing.
    """
    alpha: float = 1.0
    weights: np.ndarray | None = None
    feature_names: tuple[str, ...] = ()

    def fit(self, X: np.ndarray, y: np.ndarray) -> "RidgeRegression":
        if X.shape[0] != y.shape[0]:
            raise ValueError(f"X has {X.shape[0]} rows but y has {y.shape[0]}")
        if X.shape[1] == 0:
            raise ValueError("X has zero columns")
        # Closed-form: w = (X'X + λI)^-1 X'y
        # λ set on all features including the bias; penalizing the
        # bias slightly isn't standard practice but on this small
        # feature set it doesn't meaningfully distort results and
        # keeps the code simpler. Could split later if needed.
        XtX = X.T @ X
        reg = self.alpha * np.eye(X.shape[1])
        w = np.linalg.solve(XtX + reg, X.T @ y)
        self.weights = w
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        if self.weights is None:
            raise RuntimeError("model not fit; call fit() first")
        return X @ self.weights

    def to_dict(self) -> dict[str, Any]:
        if self.weights is None:
            raise RuntimeError("model not fit; nothing to serialize")
        return {
            "alpha": self.alpha,
            "feature_names": list(self.feature_names),
            "weights": self.weights.tolist(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RidgeRegression":
        w = np.array(payload["weights"], dtype=np.float64)
        m = cls(alpha=float(payload["alpha"]))
        m.weights = w
        m.feature_names = tuple(payload.get("feature_names", ()))
        return m


@dataclass
class TrainingReport:
    """Everything a training run produces: model, metrics, metadata.
    Serialized to JSON for downstream analysis + comparison."""
    trained_at: datetime
    total_rows: int
    train_rows: int
    test_rows: int
    alpha: float
    feature_names: list[str]
    weights: list[float]
    train_rmse: float
    test_rmse: float
    test_rank_corr: float             # Spearman rank corr with label on held-out
    heuristic_rank_corr: float        # Same metric applied to corridor_confidence, for baseline
    auc_learned: float | None         # proxy-cohort AUC of learned score (None if < 2 cohort maps)
    auc_heuristic: float | None       # same, for corridor_confidence
    auc_delta: float | None
    n_maps_learned: int
    n_maps_heuristic: int
    random_seed: int
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trained_at": self.trained_at.isoformat(),
            "total_rows": self.total_rows,
            "train_rows": self.train_rows,
            "test_rows": self.test_rows,
            "alpha": self.alpha,
            "feature_names": self.feature_names,
            "weights": self.weights,
            "train_rmse": self.train_rmse,
            "test_rmse": self.test_rmse,
            "test_rank_corr": self.test_rank_corr,
            "heuristic_rank_corr": self.heuristic_rank_corr,
            "auc_learned": self.auc_learned,
            "auc_heuristic": self.auc_heuristic,
            "auc_delta": self.auc_delta,
            "n_maps_learned": self.n_maps_learned,
            "n_maps_heuristic": self.n_maps_heuristic,
            "random_seed": self.random_seed,
            "extra": self.extra,
        }

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def rmse(pred: np.ndarray, actual: np.ndarray) -> float:
    if len(pred) == 0:
        return 0.0
    return float(np.sqrt(np.mean((pred - actual) ** 2)))


def spearman_rank_corr(a: np.ndarray, b: np.ndarray) -> float:
    """Rank correlation without scipy. NaN values in either array
    drop the pair. Returns 0.0 when there are fewer than 2 usable
    pairs (correlation undefined)."""
    if a.shape != b.shape:
        raise ValueError("a and b must have the same shape")
    mask = ~(np.isnan(a) | np.isnan(b))
    a = a[mask]
    b = b[mask]
    if a.size < 2:
        return 0.0
    # Rank (with average-rank tie-handling) via scipy isn't available;
    # numpy argsort + tie-adjust is close enough.
    a_rank = _rank_with_ties(a)
    b_rank = _rank_with_ties(b)
    a_std = a_rank.std()
    b_std = b_rank.std()
    if a_std == 0 or b_std == 0:
        return 0.0
    return float(((a_rank - a_rank.mean()) * (b_rank - b_rank.mean())).mean() / (a_std * b_std))


def _rank_with_ties(x: np.ndarray) -> np.ndarray:
    """Average-rank for ties. Each value's rank = mean of the ranks
    it would take if all duplicates were distinguishable."""
    # argsort gives positions; rank = inverse of argsort + 1
    order = x.argsort()
    ranks = np.empty_like(order, dtype=np.float64)
    ranks[order] = np.arange(1, len(x) + 1, dtype=np.float64)
    # Average-rank adjustment for ties.
    unique, counts = np.unique(x, return_counts=True)
    for val, c in zip(unique, counts):
        if c > 1:
            idx = np.where(x == val)[0]
            ranks[idx] = ranks[idx].mean()
    return ranks


def auc_roc(scores: np.ndarray, labels: np.ndarray) -> float:
    """Standard AUC: P(score_positive > score_negative). Ties count
    as 0.5 (Mann-Whitney-U formulation). Returns 0.5 when either
    class is empty."""
    pos = scores[labels == 1]
    neg = scores[labels == 0]
    if pos.size == 0 or neg.size == 0:
        return 0.5
    # Pairwise comparisons — O(|pos|·|neg|). Fine for hundreds of
    # maps; scale up later if needed.
    wins = 0.0
    for p in pos:
        wins += np.sum(p > neg) + 0.5 * np.sum(p == neg)
    return float(wins / (pos.size * neg.size))
