"""DB orchestration for persisting the learned corridor score.

Pairs with ``corridor_scoring_pipeline.py`` (heuristic) — same shape,
different score column. Reads route_corridors + evidence, runs the
learned ridge-regression model over the feature matrix, writes
``learned_corridor_score`` + provenance.

The learned score coexists with ``corridor_confidence`` by design
(see migration 019). The heuristic stays canonical until the learned
path proves out in the dry-run comparison.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pymysql.connections import Connection

from src.corridor.ranking.features import (
    build_feature_matrix,
    load_corridor_rows,
)
from src.corridor.ranking.model import RidgeRegression
from src.corridor.traversability.classification import CLASSIFICATION_VERSION
from src.corridor.traversability.evidence import _fetch_candidate_map_ids
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


# Distinct from scoring.SCORE_VERSION. Bumps independently of the
# heuristic — a learned-model refresh doesn't imply a heuristic change.
LEARNED_SCORE_VERSION: str = "learned@0.1.0"


@dataclass
class LearnedScoringStats:
    started_at: datetime
    classification_version: str
    learned_score_version: str
    model_hash: str
    maps_seen: int = 0
    maps_updated: int = 0
    corridors_scored: int = 0
    errors: list[str] = field(default_factory=list)
    completed_at: datetime | None = None

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "classification_version": self.classification_version,
            "learned_score_version": self.learned_score_version,
            "model_hash": self.model_hash,
            "maps_seen": self.maps_seen,
            "maps_updated": self.maps_updated,
            "corridors_scored": self.corridors_scored,
            "error_count": len(self.errors),
        }


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


_UPDATE_SQL = """
UPDATE route_corridors
SET learned_corridor_score = %s,
    learned_score_version = %s,
    learned_score_model_hash = %s
WHERE id = %s
"""


def compute_model_hash(model: RidgeRegression) -> str:
    """SHA-256 of the canonical model payload (weights + feature order
    + alpha). Deterministic — same model → same hash, always.

    Used as provenance on every row: a row's
    ``learned_score_model_hash`` proves which model produced it, so a
    later refresh can identify stale rows by hash mismatch.
    """
    payload = {
        "alpha": model.alpha,
        "feature_names": list(model.feature_names),
        "weights": list(model.weights.tolist()) if model.weights is not None else [],
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def load_model_from_report(report_path: Path) -> tuple[RidgeRegression, str]:
    """Load the learned model from a comparative training report.

    Prefers the ``time_envelope`` scheme when present — that's the v0.2
    target per the phase-4 sequencing. Falls back to ``inverse_rank``
    with a warning so the pipeline still runs on reports that only
    contain the baseline.

    Returns (model, label_scheme_tag) where the tag is written to
    ``learned_score_version`` (e.g. "time_envelope@0.1.0").
    """
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    time_env = payload.get("time_envelope")
    inverse = payload.get("inverse_rank")
    if time_env is not None:
        scheme = time_env
        tag = "time_envelope@0.1.0"
    elif inverse is not None:
        scheme = inverse
        tag = "inverse_rank@0.1.0"
        _LOG.warning(
            "comparative report has no time_envelope scheme — "
            "falling back to inverse_rank. Labels are weaker.",
        )
    else:
        raise RuntimeError(
            f"report at {report_path} has neither inverse_rank nor "
            "time_envelope — is this a ComparativeTrainingReport?"
        )
    model = RidgeRegression.from_dict({
        "alpha": scheme["alpha"],
        "feature_names": scheme["feature_names"],
        "weights": scheme["weights"],
    })
    return model, tag


def score_map_learned(
    conn: Connection,
    map_id: int,
    *,
    model: RidgeRegression,
    model_hash: str,
    learned_score_version: str,
    classification_version: str = CLASSIFICATION_VERSION,
) -> int:
    """Score every corridor on this map via the learned model. Returns
    the number of rows updated. Silent no-op if the map has no
    corridors for this classification_version."""
    rows = load_corridor_rows(
        conn, map_ids=[map_id], classification_version=classification_version,
    )
    if not rows:
        return 0
    vectors, X = build_feature_matrix(rows)
    if not vectors:
        return 0
    scores = model.predict(X)
    updates = [
        (float(s), learned_score_version, model_hash, v.corridor_id)
        for v, s in zip(vectors, scores)
    ]
    with cursor(conn) as cur:
        cur.executemany(_UPDATE_SQL, updates)
    conn.commit()
    return len(updates)


def score_corridors_learned(
    conn: Connection,
    *,
    model: RidgeRegression,
    learned_score_version: str = LEARNED_SCORE_VERSION,
    map_ids: Iterable[int] | None = None,
    snapshot_id: str | None = None,
    classification_version: str = CLASSIFICATION_VERSION,
    limit: int | None = None,
) -> LearnedScoringStats:
    """Set-level orchestrator — mirror of ``score_corridors`` for the
    learned model. Per-map failures captured in ``errors``."""
    model_hash = compute_model_hash(model)
    stats = LearnedScoringStats(
        started_at=_utcnow(),
        classification_version=classification_version,
        learned_score_version=learned_score_version,
        model_hash=model_hash,
    )
    if map_ids is None:
        target_ids = _fetch_candidate_map_ids(
            conn, snapshot_id=snapshot_id, limit=limit,
        )
    else:
        target_ids = [int(m) for m in map_ids]
    try:
        for mid in target_ids:
            stats.maps_seen += 1
            try:
                scored = score_map_learned(
                    conn, mid,
                    model=model, model_hash=model_hash,
                    learned_score_version=learned_score_version,
                    classification_version=classification_version,
                )
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                stats.errors.append(f"map={mid}: {exc}")
                _LOG.exception("learned scoring failed on map %d", mid)
                continue
            if scored > 0:
                stats.maps_updated += 1
                stats.corridors_scored += scored
    finally:
        stats.completed_at = _utcnow()
    return stats
