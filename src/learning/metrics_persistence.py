"""Persistence + retrieval for :mod:`src.learning.scores`-related
metrics, backed by migration 020's ``model_metrics`` table.

Separate from the ranking package because the table is consumed by
multiple systems: training writes it, the dashboard reads it, and
future tools may use the history for drift detection. Keeping
persistence here keeps the ranking code DB-free at its core.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence

from pymysql.connections import Connection

from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetricRow:
    """One row as read from ``model_metrics``. Mirrors the column
    set in migration 020; keeps nullability faithful so consumers
    don't paper over missing data."""
    id: int
    run_id: str
    recorded_at: datetime
    model_hash: str
    scheme: str
    alpha: float
    n_labeled: int
    train_rmse: float | None
    test_rmse: float | None
    test_rank_corr: float | None
    heuristic_rank_corr: float | None
    pred_stdev: float | None
    heuristic_stdev: float | None
    pred_stdev_ratio: float | None
    auc_learned: float | None
    auc_heuristic: float | None
    auc_delta: float | None
    diversity_delta_median: float | None
    diversity_delta_mean: float | None
    ai_quality_score: float | None
    variety_score: float | None
    snapshot_filter: str | None
    code_version: str
    config_hash: str


@dataclass
class MetricInsert:
    """A single pending row to persist. Typed per-field, with
    :meth:`from_training_report` + :meth:`from_scheme_report` as the
    common constructors so train-corridor-ranking's handler doesn't
    have to know the column layout."""
    run_id: str
    model_hash: str
    scheme: str
    alpha: float
    n_labeled: int
    code_version: str
    config_hash: str
    train_rmse: float | None = None
    test_rmse: float | None = None
    test_rank_corr: float | None = None
    heuristic_rank_corr: float | None = None
    pred_stdev: float | None = None
    heuristic_stdev: float | None = None
    pred_stdev_ratio: float | None = None
    auc_learned: float | None = None
    auc_heuristic: float | None = None
    auc_delta: float | None = None
    diversity_delta_median: float | None = None
    diversity_delta_mean: float | None = None
    ai_quality_score: float | None = None
    variety_score: float | None = None
    snapshot_filter: str | None = None


def new_run_id() -> str:
    """Produce a stable run_id for a single training invocation. All
    schemes from one run share the run_id so "this scheme vs that
    scheme on the same training pass" is a trivial query."""
    return uuid.uuid4().hex[:16]


_INSERT_SQL = """
INSERT INTO model_metrics (
    run_id, recorded_at, model_hash, scheme, alpha, n_labeled,
    train_rmse, test_rmse, test_rank_corr, heuristic_rank_corr,
    pred_stdev, heuristic_stdev, pred_stdev_ratio,
    auc_learned, auc_heuristic, auc_delta,
    diversity_delta_median, diversity_delta_mean,
    ai_quality_score, variety_score,
    snapshot_filter, code_version, config_hash
)
VALUES (
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s,
    %s, %s, %s
)
ON DUPLICATE KEY UPDATE
    recorded_at = VALUES(recorded_at),
    model_hash = VALUES(model_hash),
    alpha = VALUES(alpha),
    n_labeled = VALUES(n_labeled),
    train_rmse = VALUES(train_rmse),
    test_rmse = VALUES(test_rmse),
    test_rank_corr = VALUES(test_rank_corr),
    heuristic_rank_corr = VALUES(heuristic_rank_corr),
    pred_stdev = VALUES(pred_stdev),
    heuristic_stdev = VALUES(heuristic_stdev),
    pred_stdev_ratio = VALUES(pred_stdev_ratio),
    auc_learned = VALUES(auc_learned),
    auc_heuristic = VALUES(auc_heuristic),
    auc_delta = VALUES(auc_delta),
    diversity_delta_median = VALUES(diversity_delta_median),
    diversity_delta_mean = VALUES(diversity_delta_mean),
    ai_quality_score = VALUES(ai_quality_score),
    variety_score = VALUES(variety_score),
    snapshot_filter = VALUES(snapshot_filter),
    code_version = VALUES(code_version),
    config_hash = VALUES(config_hash)
"""


def record_many(conn: Connection, rows: Sequence[MetricInsert]) -> int:
    """Insert/upsert a batch of metric rows. Returns the count of
    rows supplied (NOT mysql's affected-rows count, which is
    misleading under ON DUPLICATE KEY UPDATE)."""
    if not rows:
        return 0
    now = datetime.now(tz=timezone.utc)
    payload = [
        (
            r.run_id, now, r.model_hash, r.scheme, r.alpha, r.n_labeled,
            r.train_rmse, r.test_rmse, r.test_rank_corr, r.heuristic_rank_corr,
            r.pred_stdev, r.heuristic_stdev, r.pred_stdev_ratio,
            r.auc_learned, r.auc_heuristic, r.auc_delta,
            r.diversity_delta_median, r.diversity_delta_mean,
            r.ai_quality_score, r.variety_score,
            r.snapshot_filter, r.code_version, r.config_hash,
        )
        for r in rows
    ]
    with cursor(conn) as cur:
        cur.executemany(_INSERT_SQL, payload)
    conn.commit()
    return len(payload)


def _row_to_metric(row: tuple) -> MetricRow:
    """Hand-construct a :class:`MetricRow` from a positional query tuple."""
    recorded_at = row[2]
    # pymysql returns naive datetimes; stamp UTC so downstream code doesn't
    # have to guess.
    if recorded_at is not None and recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)
    return MetricRow(
        id=int(row[0]),
        run_id=str(row[1]),
        recorded_at=recorded_at,
        model_hash=str(row[3]),
        scheme=str(row[4]),
        alpha=float(row[5]),
        n_labeled=int(row[6]),
        train_rmse=float(row[7]) if row[7] is not None else None,
        test_rmse=float(row[8]) if row[8] is not None else None,
        test_rank_corr=float(row[9]) if row[9] is not None else None,
        heuristic_rank_corr=float(row[10]) if row[10] is not None else None,
        pred_stdev=float(row[11]) if row[11] is not None else None,
        heuristic_stdev=float(row[12]) if row[12] is not None else None,
        pred_stdev_ratio=float(row[13]) if row[13] is not None else None,
        auc_learned=float(row[14]) if row[14] is not None else None,
        auc_heuristic=float(row[15]) if row[15] is not None else None,
        auc_delta=float(row[16]) if row[16] is not None else None,
        diversity_delta_median=float(row[17]) if row[17] is not None else None,
        diversity_delta_mean=float(row[18]) if row[18] is not None else None,
        ai_quality_score=float(row[19]) if row[19] is not None else None,
        variety_score=float(row[20]) if row[20] is not None else None,
        snapshot_filter=str(row[21]) if row[21] is not None else None,
        code_version=str(row[22]),
        config_hash=str(row[23]),
    )


_SELECT_COLS = (
    "id, run_id, recorded_at, model_hash, scheme, alpha, n_labeled, "
    "train_rmse, test_rmse, test_rank_corr, heuristic_rank_corr, "
    "pred_stdev, heuristic_stdev, pred_stdev_ratio, "
    "auc_learned, auc_heuristic, auc_delta, "
    "diversity_delta_median, diversity_delta_mean, "
    "ai_quality_score, variety_score, "
    "snapshot_filter, code_version, config_hash"
)


def latest_per_scheme(conn: Connection) -> dict[str, MetricRow]:
    """Most-recent row per scheme. Used by the dashboard for "current"
    readings. Returns an empty dict when no rows exist yet."""
    with cursor(conn) as cur:
        cur.execute(
            f"""
            SELECT {_SELECT_COLS}
            FROM model_metrics m
            INNER JOIN (
                SELECT scheme, MAX(recorded_at) AS latest
                FROM model_metrics
                GROUP BY scheme
            ) latest_m
              ON latest_m.scheme = m.scheme
             AND latest_m.latest = m.recorded_at
            ORDER BY m.scheme
            """,
        )
        rows = cur.fetchall()
    return {r[4]: _row_to_metric(r) for r in rows}


def history_for_scheme(
    conn: Connection, scheme: str, *, limit: int = 20,
) -> list[MetricRow]:
    """Oldest-first history for one scheme. The dashboard uses this
    for trend detection."""
    with cursor(conn) as cur:
        cur.execute(
            f"""
            SELECT * FROM (
                SELECT {_SELECT_COLS}
                FROM model_metrics
                WHERE scheme = %s
                ORDER BY recorded_at DESC
                LIMIT %s
            ) most_recent
            ORDER BY recorded_at ASC
            """,
            (scheme, int(limit)),
        )
        rows = cur.fetchall()
    return [_row_to_metric(r) for r in rows]
