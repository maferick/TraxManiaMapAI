"""DB orchestration for corridor-confidence scoring.

Reads route_corridors rows, resolves each path's edges against
traversability_edge_evidence, scores, UPDATEs the confidence +
score_version columns.

Lives in its own module (not in scoring.py) because it pulls DB
dependencies; ``scoring.py`` stays pure so tests don't need a DB.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from pymysql.connections import Connection

from src.corridor.scoring import (
    SCORE_VERSION,
    EdgeEvidence,
    score_corridor,
)
from src.corridor.traversability.classification import CLASSIFICATION_VERSION
from src.corridor.traversability.evidence import (
    _cell_to_placement_map,
    _fetch_candidate_map_ids,
    _fetch_grid_placements,
)
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


@dataclass
class ScoringStats:
    started_at: datetime
    classification_version: str
    score_version: str
    maps_seen: int = 0
    maps_updated: int = 0
    corridors_scored: int = 0
    corridors_skipped_no_edges: int = 0
    errors: list[str] = field(default_factory=list)
    completed_at: datetime | None = None

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "classification_version": self.classification_version,
            "score_version": self.score_version,
            "maps_seen": self.maps_seen,
            "maps_updated": self.maps_updated,
            "corridors_scored": self.corridors_scored,
            "corridors_skipped_no_edges": self.corridors_skipped_no_edges,
            "error_count": len(self.errors),
        }


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


_FETCH_CORRIDORS_SQL = """
SELECT id, path_cells, contains_virtual_edge
FROM route_corridors
WHERE map_id = %s AND classification_version = %s
"""

_FETCH_EVIDENCE_SQL = """
SELECT src_block_id, dst_block_id, rule_support, path_support_count,
       pattern_weight, negative_evidence_count
FROM traversability_edge_evidence
WHERE map_id = %s AND classification_version = %s
"""

_UPDATE_SCORE_SQL = """
UPDATE route_corridors
SET corridor_confidence = %s, score_version = %s
WHERE id = %s
"""


def score_map_corridors(
    conn: Connection,
    map_id: int,
    *,
    classification_version: str = CLASSIFICATION_VERSION,
    score_version: str = SCORE_VERSION,
) -> int:
    """Score every route_corridors row on this map. Returns the
    number of rows updated. Silent no-op when there are no corridors."""
    # Fetch grid placements → cell → placement_id (same resolution
    # the evidence builder uses).
    placements = _fetch_grid_placements(conn, map_id=map_id)
    if not placements:
        return 0
    cell_to_pid = _cell_to_placement_map(placements)

    # Fetch per-edge evidence for this map, keyed by (lo_pid, hi_pid).
    # Same ordered-pair convention as the evidence builder.
    with cursor(conn) as cur:
        cur.execute(_FETCH_EVIDENCE_SQL, (map_id, classification_version))
        evidence_rows = cur.fetchall()
    edge_evidence: dict[tuple[int, int], EdgeEvidence] = {}
    max_path_support = 0
    for row in evidence_rows:
        src = int(row[0])
        dst = int(row[1])
        if src >= dst:
            # Evidence-builder invariant is lo < hi; assert for safety.
            continue
        ev = EdgeEvidence(
            rule_support=bool(row[2]),
            path_support_count=int(row[3]),
            pattern_weight=float(row[4]),
            negative_evidence_count=int(row[5]),
        )
        edge_evidence[(src, dst)] = ev
        if ev.path_support_count > max_path_support:
            max_path_support = ev.path_support_count

    # Fetch corridors on this map.
    with cursor(conn) as cur:
        cur.execute(_FETCH_CORRIDORS_SQL, (map_id, classification_version))
        corridors = cur.fetchall()
    if not corridors:
        return 0

    updates: list[tuple[float, str, int]] = []
    for corridor_row in corridors:
        rc_id = int(corridor_row[0])
        cells_json = corridor_row[1]
        contains_virtual = bool(corridor_row[2])
        try:
            cells = [tuple(c) for c in json.loads(cells_json)]
        except (TypeError, json.JSONDecodeError):
            continue
        # Resolve each edge (consecutive cell pair) to its evidence.
        edge_evidences: list[EdgeEvidence] = []
        for i in range(len(cells) - 1):
            pid_a = cell_to_pid.get(cells[i])
            pid_b = cell_to_pid.get(cells[i + 1])
            if pid_a is None or pid_b is None:
                # Either cell not in placements — typically a virtual-
                # edge hop between distant anchor cells. Skip this
                # edge in the min(); the virtual-edge factor handles
                # the overall downweight.
                continue
            lo, hi = (pid_a, pid_b) if pid_a < pid_b else (pid_b, pid_a)
            ev = edge_evidence.get((lo, hi))
            if ev is None:
                # Grid-adjacent edge with no evidence row — shouldn't
                # happen if evidence was built at this classification
                # version. Skip defensively.
                continue
            edge_evidences.append(ev)
        confidence = score_corridor(
            edge_evidences,
            contains_virtual_edge=contains_virtual,
            per_map_max_path_support=max_path_support,
        )
        updates.append((confidence, score_version, rc_id))

    if updates:
        with cursor(conn) as cur:
            cur.executemany(_UPDATE_SCORE_SQL, updates)
        conn.commit()
    return len(updates)


def score_corridors(
    conn: Connection,
    map_ids: Iterable[int] | None = None,
    *,
    snapshot_id: str | None = None,
    classification_version: str = CLASSIFICATION_VERSION,
    score_version: str = SCORE_VERSION,
    limit: int | None = None,
) -> ScoringStats:
    """Set-level orchestrator. Per-map failures captured in errors."""
    stats = ScoringStats(
        started_at=_utcnow(),
        classification_version=classification_version,
        score_version=score_version,
    )
    target_ids: list[int]
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
                scored = score_map_corridors(
                    conn, mid,
                    classification_version=classification_version,
                    score_version=score_version,
                )
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                stats.errors.append(f"map={mid}: {exc}")
                _LOG.exception("scoring failed on map %d", mid)
                continue
            if scored > 0:
                stats.maps_updated += 1
                stats.corridors_scored += scored
    finally:
        stats.completed_at = _utcnow()
    return stats
