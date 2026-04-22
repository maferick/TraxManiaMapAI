"""Training orchestration + heuristic comparison for corridor ranking.

Takes a DB connection, materializes features + labels, splits 80/20
deterministically, fits ridge regression, evaluates, and builds a
:class:`TrainingReport` with both learned-model and
``corridor_confidence`` baseline metrics.

Per-map aggregation for the AUC comparison:

- Both the learned model and the heuristic produce PER-CORRIDOR
  scores. For proxy-cohort AUC we need a PER-MAP score.
- Aggregation: mean across the map's corridors. Simple, symmetric
  across both models, compares like-for-like.

Label caveat: the inverse-rank label is a proxy, not truth. The
AUC comparison tells us which score separates the popularity-
biased proxy cohorts better — which is useful directionally, but
neither score "wins" on ground truth because we don't have any.
"""
from __future__ import annotations

import logging
from typing import Sequence

import numpy as np
from pymysql.connections import Connection

from src.corridor.ranking.features import (
    FEATURE_NAMES,
    CorridorFeatureVector,
    CorridorRow,
    build_feature_matrix,
    load_corridor_rows,
)
from src.corridor.ranking.labels import synthesize_inverse_rank_labels
from src.corridor.ranking.model import (
    RidgeRegression,
    TrainingReport,
    auc_roc,
    rmse,
    spearman_rank_corr,
)
from src.corridor.traversability.classification import CLASSIFICATION_VERSION
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


def _fetch_cohort_map_ids(
    conn: Connection, benchmark_path_substrings: Sequence[str]
) -> set[int]:
    """Return the set of internal map_ids referenced by any manifest
    matching ``benchmark_path_substrings``. Used to build the
    positive/negative proxy sets for AUC comparison."""
    # Soft import — `src.benchmarks` isn't always loaded in this path.
    from src.benchmarks.manifest import load as load_benchmark
    from pathlib import Path

    ids: set[int] = set()
    for substr in benchmark_path_substrings:
        matching = list(Path("data/benchmarks").rglob(f"*{substr}*.yaml"))
        for path in matching:
            try:
                manifest = load_benchmark(path)
            except Exception:  # noqa: BLE001
                continue
            source_ids = [str(e.map_id) for e in manifest.entries]
            if not source_ids:
                continue
            placeholders = ",".join(["%s"] * len(source_ids))
            with cursor(conn) as cur:
                cur.execute(
                    f"SELECT id FROM maps WHERE source_map_id IN ({placeholders})",
                    tuple(source_ids),
                )
                ids.update(int(r[0]) for r in cur.fetchall())
    return ids


def _deterministic_split(
    n: int, test_frac: float, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    """Return (train_idx, test_idx). Seed-driven for reproducibility."""
    rng = np.random.default_rng(seed)
    idx = np.arange(n)
    rng.shuffle(idx)
    cut = int(n * (1 - test_frac))
    return idx[:cut], idx[cut:]


def _per_map_mean(
    vectors: list[CorridorFeatureVector],
    scores: np.ndarray,
) -> dict[int, float]:
    """Aggregate per-corridor scores into per-map means."""
    by_map: dict[int, list[float]] = {}
    for vec, score in zip(vectors, scores):
        by_map.setdefault(vec.map_id, []).append(float(score))
    return {m: (sum(s) / len(s)) for m, s in by_map.items() if s}


def _build_cohort_auc(
    per_map_score: dict[int, float],
    pos_ids: set[int],
    neg_ids: set[int],
) -> tuple[float | None, int]:
    pos_scores = [per_map_score[m] for m in pos_ids if m in per_map_score]
    neg_scores = [per_map_score[m] for m in neg_ids if m in per_map_score]
    scored_count = len(pos_scores) + len(neg_scores)
    if not pos_scores or not neg_scores:
        return None, scored_count
    scores = np.array(pos_scores + neg_scores, dtype=np.float64)
    labels = np.array([1] * len(pos_scores) + [0] * len(neg_scores), dtype=np.int64)
    return auc_roc(scores, labels), scored_count


def train_and_evaluate(
    conn: Connection,
    *,
    alpha: float = 1.0,
    test_frac: float = 0.2,
    random_seed: int = 42,
    map_ids: Sequence[int] | None = None,
    classification_version: str = CLASSIFICATION_VERSION,
    pos_benchmark_substr: Sequence[str] = ("tech-strong-proxy",),
    neg_benchmark_substr: Sequence[str] = ("tech-mediocre-proxy",),
) -> TrainingReport:
    """End-to-end train + evaluate."""
    from src.corridor.ranking.model import _utcnow

    rows: list[CorridorRow] = load_corridor_rows(
        conn, map_ids=map_ids, classification_version=classification_version,
    )
    if not rows:
        raise RuntimeError("no corridor rows to train on — did you run build-route-corridors?")

    vectors, X = build_feature_matrix(rows)
    label_by_id = synthesize_inverse_rank_labels(rows)
    y = np.array(
        [label_by_id[v.corridor_id] for v in vectors], dtype=np.float64,
    )

    train_idx, test_idx = _deterministic_split(
        n=len(vectors), test_frac=test_frac, seed=random_seed,
    )
    X_train, X_test = X[train_idx], X[test_idx]
    y_train, y_test = y[train_idx], y[test_idx]

    model = RidgeRegression(alpha=alpha, feature_names=FEATURE_NAMES)
    model.fit(X_train, y_train)
    assert model.weights is not None

    pred_train = model.predict(X_train)
    pred_test = model.predict(X_test)

    # Baseline: corridor_confidence as a direct score. Use whatever
    # the heuristic produced (may be None if unscored).
    heuristic_scores_test = np.array(
        [
            (vectors[i].corridor_confidence
             if vectors[i].corridor_confidence is not None else float("nan"))
            for i in test_idx
        ],
        dtype=np.float64,
    )

    # Proxy-cohort AUC comparison — across ALL scored corridors,
    # not just the test split. (Proxy cohorts are small relative
    # to the test split, so we want every available score.)
    pos_ids = _fetch_cohort_map_ids(conn, pos_benchmark_substr)
    neg_ids = _fetch_cohort_map_ids(conn, neg_benchmark_substr)
    learned_scores_all = model.predict(X)
    learned_per_map = _per_map_mean(vectors, learned_scores_all)

    heuristic_scores_all = np.array(
        [(v.corridor_confidence if v.corridor_confidence is not None else float("nan"))
         for v in vectors],
        dtype=np.float64,
    )
    # Drop NaN corridors before per-map averaging
    heuristic_valid = [(v, s) for v, s in zip(vectors, heuristic_scores_all) if not np.isnan(s)]
    heuristic_per_map: dict[int, float] = {}
    if heuristic_valid:
        _vecs, _sc = zip(*heuristic_valid)
        heuristic_per_map = _per_map_mean(list(_vecs), np.array(_sc))

    auc_learned, n_l = _build_cohort_auc(learned_per_map, pos_ids, neg_ids)
    auc_heuristic, n_h = _build_cohort_auc(heuristic_per_map, pos_ids, neg_ids)

    return TrainingReport(
        trained_at=_utcnow(),
        total_rows=len(vectors),
        train_rows=len(train_idx),
        test_rows=len(test_idx),
        alpha=alpha,
        feature_names=list(FEATURE_NAMES),
        weights=model.weights.tolist(),
        train_rmse=rmse(pred_train, y_train),
        test_rmse=rmse(pred_test, y_test),
        test_rank_corr=spearman_rank_corr(pred_test, y_test),
        heuristic_rank_corr=spearman_rank_corr(heuristic_scores_test, y[test_idx]),
        auc_learned=auc_learned,
        auc_heuristic=auc_heuristic,
        auc_delta=(
            auc_learned - auc_heuristic
            if (auc_learned is not None and auc_heuristic is not None) else None
        ),
        n_maps_learned=n_l,
        n_maps_heuristic=n_h,
        random_seed=random_seed,
    )
