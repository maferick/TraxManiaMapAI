"""Training orchestration + heuristic comparison for corridor ranking.

Takes a DB connection, materializes features + labels, splits 80/20
deterministically, fits ridge regression, evaluates, and builds a
:class:`ComparativeTrainingReport` with:

- v0.1 ``inverse_rank`` baseline — synthetic label, always trainable
- v0.2 ``time_envelope`` — labels from per-map replay mean inter-CP
  elapsed times; only covers corridors on maps with clean replays

Both models use the same features, the same deterministic 80/20 split
(same seed), and the same heuristic baseline — so the delta between
them is purely a function of label quality, not training discipline.

Per-map aggregation for the AUC comparison:

- Both the learned model and the heuristic produce PER-CORRIDOR
  scores. For proxy-cohort AUC we need a PER-MAP score.
- Aggregation: mean across the map's corridors. Simple, symmetric
  across both models, compares like-for-like.

Label caveat: NEITHER label is ground truth.

- ``inverse_rank`` encodes "shortest + lex tiebreak" — a convention,
  not truth.
- ``time_envelope`` encodes "corridor length fits the elapsed time an
  actual driver took between checkpoints, under a global speed prior."
  This is an OBSERVED WEAK LABEL: it rewards corridors that are
  length-plausible given observed behavior, but the speed prior is
  constant and the per-interval time is map-averaged (we can't align
  individual CP gaps to individual CP blocks yet). Better than
  inverse-rank, not ground truth.
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
    ComparativeTrainingReport,
    RidgeRegression,
    TrainingReport,
    auc_roc,
    rmse,
    spearman_rank_corr,
)
from src.corridor.ranking.time_envelope_labels import (
    _load_map_mean_interval_ms,
    synthesize_time_envelope_labels,
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


def _train_one_scheme(
    *,
    label_scheme: str,
    vectors: list[CorridorFeatureVector],
    X: np.ndarray,
    label_by_id: dict[int, float],
    alpha: float,
    test_frac: float,
    random_seed: int,
    pos_ids: set[int],
    neg_ids: set[int],
) -> TrainingReport | None:
    """Train one label scheme. Returns None when ``label_by_id`` is
    empty — the caller treats that as "scheme inapplicable."

    Corridors whose id isn't in ``label_by_id`` are dropped from the
    training set for this scheme. For ``inverse_rank`` this is always
    all corridors (every corridor gets a label). For ``time_envelope``
    it's only corridors on maps with clean replays.
    """
    from src.corridor.ranking.model import _utcnow

    if not label_by_id:
        return None

    # Restrict to labeled corridors. Same feature columns, different
    # row selection across schemes.
    keep_mask = np.array(
        [v.corridor_id in label_by_id for v in vectors], dtype=bool,
    )
    if not keep_mask.any():
        return None
    kept_vectors = [v for v, k in zip(vectors, keep_mask) if k]
    X_kept = X[keep_mask]
    y = np.array([label_by_id[v.corridor_id] for v in kept_vectors], dtype=np.float64)

    train_idx, test_idx = _deterministic_split(
        n=len(kept_vectors), test_frac=test_frac, seed=random_seed,
    )
    X_train, X_test = X_kept[train_idx], X_kept[test_idx]
    y_train, y_test = y[train_idx], y[test_idx]

    model = RidgeRegression(alpha=alpha, feature_names=FEATURE_NAMES)
    model.fit(X_train, y_train)
    assert model.weights is not None

    pred_train = model.predict(X_train)
    pred_test = model.predict(X_test)

    heuristic_scores_test = np.array(
        [
            (kept_vectors[i].corridor_confidence
             if kept_vectors[i].corridor_confidence is not None else float("nan"))
            for i in test_idx
        ],
        dtype=np.float64,
    )

    # Proxy-cohort AUC over ALL scored corridors in this scheme's
    # labeled set, not just the test split.
    learned_scores_all = model.predict(X_kept)
    learned_per_map = _per_map_mean(kept_vectors, learned_scores_all)

    heuristic_scores_all = np.array(
        [(v.corridor_confidence if v.corridor_confidence is not None else float("nan"))
         for v in kept_vectors],
        dtype=np.float64,
    )
    heuristic_valid = [
        (v, s) for v, s in zip(kept_vectors, heuristic_scores_all) if not np.isnan(s)
    ]
    heuristic_per_map: dict[int, float] = {}
    if heuristic_valid:
        _vecs, _sc = zip(*heuristic_valid)
        heuristic_per_map = _per_map_mean(list(_vecs), np.array(_sc))

    auc_learned, n_l = _build_cohort_auc(learned_per_map, pos_ids, neg_ids)
    auc_heuristic, n_h = _build_cohort_auc(heuristic_per_map, pos_ids, neg_ids)

    return TrainingReport(
        label_scheme=label_scheme,
        trained_at=_utcnow(),
        total_rows=len(kept_vectors),
        train_rows=len(train_idx),
        test_rows=len(test_idx),
        alpha=alpha,
        feature_names=list(FEATURE_NAMES),
        weights=model.weights.tolist(),
        train_rmse=rmse(pred_train, y_train),
        test_rmse=rmse(pred_test, y_test),
        test_rank_corr=spearman_rank_corr(pred_test, y_test),
        heuristic_rank_corr=spearman_rank_corr(heuristic_scores_test, y_test),
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
) -> ComparativeTrainingReport:
    """End-to-end train + evaluate both label schemes."""
    rows: list[CorridorRow] = load_corridor_rows(
        conn, map_ids=map_ids, classification_version=classification_version,
    )
    if not rows:
        raise RuntimeError(
            "no corridor rows to train on — did you run build-route-corridors?"
        )

    vectors, X = build_feature_matrix(rows)

    # Proxy cohorts are label-scheme-independent.
    pos_ids = _fetch_cohort_map_ids(conn, pos_benchmark_substr)
    neg_ids = _fetch_cohort_map_ids(conn, neg_benchmark_substr)

    # v0.1 — synthetic inverse-rank labels (always available).
    inverse_rank_labels = synthesize_inverse_rank_labels(rows)
    inverse_rank_report = _train_one_scheme(
        label_scheme="inverse_rank",
        vectors=vectors, X=X,
        label_by_id=inverse_rank_labels,
        alpha=alpha, test_frac=test_frac, random_seed=random_seed,
        pos_ids=pos_ids, neg_ids=neg_ids,
    )
    assert inverse_rank_report is not None  # every corridor gets a label

    # v0.2 — time-envelope labels (only maps with clean replays).
    map_mean_interval_ms = _load_map_mean_interval_ms(conn)
    time_envelope_labels = synthesize_time_envelope_labels(
        rows, map_mean_interval_ms=map_mean_interval_ms,
    )
    time_envelope_report = _train_one_scheme(
        label_scheme="time_envelope",
        vectors=vectors, X=X,
        label_by_id=time_envelope_labels,
        alpha=alpha, test_frac=test_frac, random_seed=random_seed,
        pos_ids=pos_ids, neg_ids=neg_ids,
    )
    if time_envelope_report is None:
        _LOG.warning(
            "time-envelope labels unavailable — no maps had mean "
            "inter-CP intervals. Only inverse-rank trained.",
        )

    return ComparativeTrainingReport(
        inverse_rank=inverse_rank_report,
        time_envelope=time_envelope_report,
        map_mean_interval_ms_count=len(map_mean_interval_ms),
    )
