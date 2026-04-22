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

    def fit(
        self,
        X: np.ndarray,
        y: np.ndarray,
        *,
        sample_weights: np.ndarray | None = None,
    ) -> "RidgeRegression":
        """Closed-form ridge fit. Optional per-sample weights.

        Unweighted (default):     w* = (XᵀX + λI)⁻¹ Xᵀy
        Weighted (sample_weights): w* = (XᵀWX + λI)⁻¹ XᵀWy

        Weighted path uses the row-scaling trick — multiply each row
        of X and each y by ``√s_i``. Algebraically identical to the
        diag-matrix form; numerically cleaner and avoids building a
        dense diagonal at our scale.

        Sample weights must be non-negative. Zero weights are valid
        (sample ignored); negative weights raise.
        """
        if X.shape[0] != y.shape[0]:
            raise ValueError(f"X has {X.shape[0]} rows but y has {y.shape[0]}")
        if X.shape[1] == 0:
            raise ValueError("X has zero columns")
        # λ set on all features including the bias; penalizing the
        # bias slightly isn't standard practice but on this small
        # feature set it doesn't meaningfully distort results and
        # keeps the code simpler. Could split later if needed.
        if sample_weights is None:
            XtX = X.T @ X
            Xty = X.T @ y
        else:
            if sample_weights.shape != (X.shape[0],):
                raise ValueError(
                    f"sample_weights shape {sample_weights.shape} "
                    f"doesn't match X rows {X.shape[0]}"
                )
            if np.any(sample_weights < 0):
                raise ValueError("sample_weights must be non-negative")
            sqrt_w = np.sqrt(sample_weights)
            Xw = X * sqrt_w[:, None]
            yw = y * sqrt_w
            XtX = Xw.T @ Xw
            Xty = Xw.T @ yw
        reg = self.alpha * np.eye(X.shape[1])
        self.weights = np.linalg.solve(XtX + reg, Xty)
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
    Serialized to JSON for downstream analysis + comparison.

    One ``TrainingReport`` = one label scheme. A run that trains both
    the inverse-rank baseline and a time-envelope model emits a
    :class:`ComparativeTrainingReport` holding two of these.
    """
    label_scheme: str                 # "inverse_rank" | "time_envelope"
    trained_at: datetime
    total_rows: int                   # rows LABELED for this scheme (may differ across schemes)
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
            "label_scheme": self.label_scheme,
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


@dataclass
class ComparativeTrainingReport:
    """Pair of TrainingReports — the inverse-rank baseline and the
    time-envelope model, trained on the same feature matrix and split
    but different label schemes. Enables head-to-head comparison of
    whether a behavior-grounded label shifts model behavior.

    ``time_envelope`` may be None when no clean replays supply
    checkpoint times on any corridor-owning map (the label set would be
    empty). In that case only the baseline trains.
    """
    inverse_rank: TrainingReport
    time_envelope: TrainingReport | None
    map_mean_interval_ms_count: int   # how many maps contributed a mean interval time

    def to_dict(self) -> dict[str, Any]:
        return {
            "inverse_rank": self.inverse_rank.to_dict(),
            "time_envelope": (
                self.time_envelope.to_dict() if self.time_envelope is not None else None
            ),
            "map_mean_interval_ms_count": self.map_mean_interval_ms_count,
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
