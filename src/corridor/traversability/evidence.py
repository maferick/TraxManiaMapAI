"""Persistence pipeline for `traversability_edge_evidence`.

Materializes per-map per-edge labels into the table defined by
``migrations/mariadb/016_traversability_edge_evidence.sql``.

Scope — v0.1 populates the two columns that can be computed purely
from classification and grid adjacency:

- ``rule_support`` — True iff the edge is ``seed_valid`` (both
  endpoints in ``DRIVABLE_FAMILIES``).
- ``traversability_state`` — one of ``seed_valid``, ``unsupported``,
  ``unknown`` (the ``supported`` state is reserved for Phase-3 signals
  that aren't wired yet).

The three signal columns (``path_support_count``, ``pattern_weight``,
``negative_evidence_count``) stay at their schema defaults of 0 in
v0.1; later phases will bump them via separate update paths.

Idempotency: the pipeline deletes the map's existing rows for the
current ``classification_version`` before inserting, so re-runs at
the same version are exact replacements. Different classification
versions coexist via the ``uq_trv_ev_edge`` unique key.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from pymysql.connections import Connection

from src.corridor.traversability.classification import (
    CLASSIFICATION_VERSION,
    FamilyBucket,
    classify_family,
)
from src.corridor.traversability.labeling import label_edge
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)

# Batch size for executemany. Large enough to amortize round-trips,
# small enough to avoid MySQL max_allowed_packet issues on wide rows.
_INSERT_BATCH_SIZE: int = 5000


@dataclass
class EvidenceBuildStats:
    """Aggregate counters from a build run."""
    started_at: datetime
    classification_version: str
    maps_seen: int = 0
    maps_written: int = 0
    maps_skipped_no_placements: int = 0
    edges_written: int = 0
    seed_valid: int = 0
    unsupported: int = 0
    unknown: int = 0
    errors: list[str] = field(default_factory=list)
    completed_at: datetime | None = None

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "classification_version": self.classification_version,
            "maps_seen": self.maps_seen,
            "maps_written": self.maps_written,
            "maps_skipped_no_placements": self.maps_skipped_no_placements,
            "edges_written": self.edges_written,
            "seed_valid": self.seed_valid,
            "unsupported": self.unsupported,
            "unknown": self.unknown,
            "error_count": len(self.errors),
        }


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _fetch_candidate_map_ids(
    conn: Connection, *, snapshot_id: str | None, limit: int | None,
) -> list[int]:
    """Maps with parse_status=success. snapshot_id narrows to one
    ingestion run when set."""
    sql = "SELECT id FROM maps WHERE parse_status = 'success'"
    params: list[Any] = []
    if snapshot_id is not None:
        sql += " AND ingestion_snapshot = %s"
        params.append(snapshot_id)
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(int(limit))
    with cursor(conn) as cur:
        cur.execute(sql, tuple(params))
        return [int(r[0]) for r in cur.fetchall()]


def _fetch_grid_placements(
    conn: Connection, *, map_id: int
) -> list[tuple[int, int, int, int, str]]:
    """Return ``(placement_id, x, y, z, block_family)`` for each grid
    block on the map. placement_id is used as the evidence table's
    src_block_id / dst_block_id so the edges are addressable by
    canonical block_placements row id.
    """
    with cursor(conn) as cur:
        cur.execute(
            "SELECT id, x, y, z, block_family FROM block_placements "
            "WHERE map_id = %s AND is_free = 0 "
            "AND x IS NOT NULL AND y IS NOT NULL AND z IS NOT NULL",
            (map_id,),
        )
        return [
            (int(r[0]), int(r[1]), int(r[2]), int(r[3]), str(r[4] or ""))
            for r in cur.fetchall()
        ]


_DELETE_MAP_ROWS_SQL = (
    "DELETE FROM traversability_edge_evidence "
    "WHERE map_id = %s AND classification_version = %s"
)

_INSERT_SQL = """
INSERT INTO traversability_edge_evidence (
    map_id, src_block_id, dst_block_id,
    traversability_state, rule_support, classification_version
) VALUES (%s, %s, %s, %s, %s, %s)
"""


def _build_rows_for_map(
    placements: list[tuple[int, int, int, int, str]],
    classification_version: str,
    map_id: int,
) -> tuple[list[tuple[Any, ...]], dict[str, int]]:
    """For one map, enumerate axis-neighbor edges, classify each, and
    emit the insert tuples. Family-priority promotion on layered
    placements mirrors _build_cell_graph: DRIVABLE > AMBIGUOUS >
    NON_DRIVABLE when multiple blocks share a cell.

    Returns (rows, state_counts) where state_counts maps each state
    to its row count for this map.
    """
    # Resolve each (x, y, z) cell to a single (placement_id, family).
    # The cell lookup uses the same priority rule as the reachability
    # path so the two stay consistent — a cell with (Structure, Road)
    # at the same coord resolves to Road in both places.
    cell_to: dict[tuple[int, int, int], tuple[int, str, FamilyBucket]] = {}
    for pid, x, y, z, family in placements:
        cell = (x, y, z)
        new_bucket = classify_family(family)
        existing = cell_to.get(cell)
        if existing is None:
            cell_to[cell] = (pid, family, new_bucket)
            continue
        _, _, existing_bucket = existing
        if (existing_bucket is FamilyBucket.NON_DRIVABLE
                and new_bucket is not FamilyBucket.NON_DRIVABLE):
            cell_to[cell] = (pid, family, new_bucket)
        elif (existing_bucket is FamilyBucket.AMBIGUOUS
              and new_bucket is FamilyBucket.DRIVABLE):
            cell_to[cell] = (pid, family, new_bucket)

    rows: list[tuple[Any, ...]] = []
    state_counts = {"seed_valid": 0, "unsupported": 0, "unknown": 0}
    seen_pairs: set[tuple[int, int]] = set()
    for cell, (pid, family, _bucket) in cell_to.items():
        x, y, z = cell
        for nx, ny, nz in (
            (x + 1, y, z), (x - 1, y, z),
            (x, y + 1, z), (x, y - 1, z),
            (x, y, z + 1), (x, y, z - 1),
        ):
            nb_entry = cell_to.get((nx, ny, nz))
            if nb_entry is None:
                continue
            nb_pid, nb_family, _nb_bucket = nb_entry
            # Dedupe by ordered id-pair so each axis edge yields one row.
            lo, hi = (pid, nb_pid) if pid < nb_pid else (nb_pid, pid)
            if lo == hi:
                # Self-adjacency (degenerate) — skip; the schema's
                # unique key would accept it but it carries no info.
                continue
            if (lo, hi) in seen_pairs:
                continue
            seen_pairs.add((lo, hi))
            label = label_edge(family, nb_family)
            state_counts[label.state] = state_counts.get(label.state, 0) + 1
            rows.append((
                map_id,
                lo, hi,
                label.state,
                int(label.rule_support),
                classification_version,
            ))
    return rows, state_counts


def build_map_evidence(
    conn: Connection,
    map_id: int,
    *,
    classification_version: str = CLASSIFICATION_VERSION,
    batch_size: int = _INSERT_BATCH_SIZE,
) -> dict[str, int]:
    """Build + persist evidence for one map. Returns the state-count
    dict for this map. Raises on DB errors so the caller can record
    them against stage_run errors.
    """
    placements = _fetch_grid_placements(conn, map_id=map_id)
    if not placements:
        return {"seed_valid": 0, "unsupported": 0, "unknown": 0}
    rows, counts = _build_rows_for_map(
        placements, classification_version, map_id,
    )
    with cursor(conn) as cur:
        cur.execute(_DELETE_MAP_ROWS_SQL, (map_id, classification_version))
        for start in range(0, len(rows), batch_size):
            chunk = rows[start:start + batch_size]
            cur.executemany(_INSERT_SQL, chunk)
    conn.commit()
    return counts


_UPDATE_PATH_SUPPORT_SQL = """
UPDATE traversability_edge_evidence
SET path_support_count = %s
WHERE map_id = %s
  AND src_block_id = %s
  AND dst_block_id = %s
  AND classification_version = %s
"""

_UPDATE_NEGATIVE_EVIDENCE_SQL = """
UPDATE traversability_edge_evidence
SET negative_evidence_count = %s
WHERE map_id = %s
  AND src_block_id = %s
  AND dst_block_id = %s
  AND classification_version = %s
"""

# Signal 4 threshold: an edge's endpoints have six axis-neighbors each
# (12 total). Edges with ≥ this many NON_DRIVABLE surrounding cells
# are flagged as "in a deco cluster" — technically drivable under
# classification but in a neighborhood that racing corridors
# shouldn't prefer. 6 of 12 = 50% of surrounding cells are deco.
DECO_CLUSTER_NEIGHBOR_THRESHOLD: int = 6


@dataclass
class PathSupportStats:
    """Counters from a Signal-1 update run."""
    started_at: datetime
    classification_version: str
    maps_seen: int = 0
    maps_updated: int = 0
    maps_skipped_no_evidence: int = 0
    intervals_enumerated: int = 0
    edges_updated: int = 0
    path_count_total: int = 0
    errors: list[str] = field(default_factory=list)
    completed_at: datetime | None = None

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "classification_version": self.classification_version,
            "maps_seen": self.maps_seen,
            "maps_updated": self.maps_updated,
            "maps_skipped_no_evidence": self.maps_skipped_no_evidence,
            "intervals_enumerated": self.intervals_enumerated,
            "edges_updated": self.edges_updated,
            "path_count_total": self.path_count_total,
            "error_count": len(self.errors),
        }


def _cell_to_placement_map(
    placements: list[tuple[int, int, int, int, str]],
) -> dict[tuple[int, int, int], int]:
    """Resolve each (x, y, z) cell to a single placement_id using the
    same priority rule as _build_rows_for_map (DRIVABLE > AMBIGUOUS >
    NON_DRIVABLE). Mirrors the evidence-row build so the path-support
    update hits the same rows that exist in the evidence table."""
    cell_map: dict[tuple[int, int, int], tuple[int, FamilyBucket]] = {}
    for pid, x, y, z, family in placements:
        cell = (x, y, z)
        bucket = classify_family(family)
        existing = cell_map.get(cell)
        if existing is None:
            cell_map[cell] = (pid, bucket)
            continue
        _, existing_bucket = existing
        if (existing_bucket is FamilyBucket.NON_DRIVABLE
                and bucket is not FamilyBucket.NON_DRIVABLE):
            cell_map[cell] = (pid, bucket)
        elif (existing_bucket is FamilyBucket.AMBIGUOUS
              and bucket is FamilyBucket.DRIVABLE):
            cell_map[cell] = (pid, bucket)
    return {cell: pid for cell, (pid, _) in cell_map.items()}


def update_path_support_for_map(
    conn: Connection,
    map_id: int,
    *,
    classification_version: str = CLASSIFICATION_VERSION,
) -> tuple[int, int, int]:
    """Enumerate corridor paths for this map, count how often each
    real grid edge is traversed, and UPDATE path_support_count on the
    matching evidence rows. Virtual (observation-derived) edges are
    excluded from counts — only edges that exist in
    traversability_edge_evidence get updated.

    Returns (intervals_enumerated, edges_updated, path_count_total).
    Raises on DB errors so the caller can record them.
    """
    # Local import to avoid a circular reference (evidence ↔ enumeration).
    from src.corridor.traversability.enumeration import enumerate_map

    intervals = enumerate_map(conn, map_id, keep_paths=True)
    if not intervals:
        return 0, 0, 0

    placements = _fetch_grid_placements(conn, map_id=map_id)
    cell_to_pid = _cell_to_placement_map(placements)

    # Accumulate (lo_pid, hi_pid) → count across all paths in all
    # intervals for this map. An edge used in two paths of two
    # different intervals counts twice — that's the Signal 1 intent
    # ("edges recurring in plausible paths gain weight").
    edge_counts: dict[tuple[int, int], int] = {}
    path_count_total = 0
    for iv in intervals:
        for path in iv.paths:
            path_count_total += 1
            for i in range(len(path) - 1):
                pid_a = cell_to_pid.get(path[i])
                pid_b = cell_to_pid.get(path[i + 1])
                if pid_a is None or pid_b is None:
                    # Virtual-edge endpoints may map to pids we did
                    # resolve, but the cell→pid step above uses the
                    # same policy as the evidence builder, so a legit
                    # grid edge must have both pids. Missing-pid =
                    # cell never made it into block_placements at
                    # all, which shouldn't happen — defensive skip.
                    continue
                if pid_a == pid_b:
                    continue
                lo, hi = (pid_a, pid_b) if pid_a < pid_b else (pid_b, pid_a)
                edge_counts[(lo, hi)] = edge_counts.get((lo, hi), 0) + 1

    if not edge_counts:
        return len(intervals), 0, path_count_total

    # executemany UPDATE. Rows that don't exist in the evidence table
    # (virtual-edge pairs that never hit a grid adjacency) silently
    # match zero rows — that's fine, UPDATE is a no-op there.
    params = [
        (count, map_id, lo, hi, classification_version)
        for (lo, hi), count in edge_counts.items()
    ]
    with cursor(conn) as cur:
        cur.executemany(_UPDATE_PATH_SUPPORT_SQL, params)
    conn.commit()
    return len(intervals), len(edge_counts), path_count_total


@dataclass
class NegativeEvidenceStats:
    """Counters for a Signal-4 update run."""
    started_at: datetime
    classification_version: str
    maps_seen: int = 0
    maps_updated: int = 0
    edges_examined: int = 0
    edges_flagged: int = 0
    errors: list[str] = field(default_factory=list)
    completed_at: datetime | None = None

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "classification_version": self.classification_version,
            "maps_seen": self.maps_seen,
            "maps_updated": self.maps_updated,
            "edges_examined": self.edges_examined,
            "edges_flagged": self.edges_flagged,
            "error_count": len(self.errors),
        }


def _count_non_drivable_neighbors(
    cell: tuple[int, int, int],
    cell_bucket: dict[tuple[int, int, int], FamilyBucket],
) -> int:
    """Return how many of the 6 axis-neighbors of ``cell`` are
    NON_DRIVABLE. Cells that aren't placed at all (no block_placements
    row there) count as 0 — absence isn't evidence of non-drivability.
    """
    x, y, z = cell
    count = 0
    for nx, ny, nz in (
        (x + 1, y, z), (x - 1, y, z),
        (x, y + 1, z), (x, y - 1, z),
        (x, y, z + 1), (x, y, z - 1),
    ):
        bucket = cell_bucket.get((nx, ny, nz))
        if bucket is FamilyBucket.NON_DRIVABLE:
            count += 1
    return count


def update_negative_evidence_for_map(
    conn: Connection,
    map_id: int,
    *,
    classification_version: str = CLASSIFICATION_VERSION,
    threshold: int = DECO_CLUSTER_NEIGHBOR_THRESHOLD,
) -> tuple[int, int]:
    """For each evidence row on this map, compute the combined
    NON_DRIVABLE-neighbor count across both endpoint cells. When the
    total meets ``threshold`` (default 6 of 12 = 50%), increment
    ``negative_evidence_count``.

    Non-drivable neighbors are measured from the full placements grid
    — not limited to the evidence table — so the count captures deco
    clustering even when the edges themselves are already labeled
    unsupported. Returns (edges_examined, edges_flagged).
    """
    placements = _fetch_grid_placements(conn, map_id=map_id)
    if not placements:
        return 0, 0
    cell_to_pid = _cell_to_placement_map(placements)
    cell_bucket: dict[tuple[int, int, int], FamilyBucket] = {}
    for pid, x, y, z, family in placements:
        cell = (x, y, z)
        bucket = classify_family(family)
        existing = cell_bucket.get(cell)
        if existing is None:
            cell_bucket[cell] = bucket
            continue
        # Same promotion as cell_to_pid for consistency.
        if (existing is FamilyBucket.NON_DRIVABLE
                and bucket is not FamilyBucket.NON_DRIVABLE):
            cell_bucket[cell] = bucket
        elif (existing is FamilyBucket.AMBIGUOUS
              and bucket is FamilyBucket.DRIVABLE):
            cell_bucket[cell] = bucket

    # Load existing edges from the evidence table. Only rows at this
    # classification_version participate.
    with cursor(conn) as cur:
        cur.execute(
            "SELECT src_block_id, dst_block_id FROM traversability_edge_evidence "
            "WHERE map_id = %s AND classification_version = %s",
            (map_id, classification_version),
        )
        edges = cur.fetchall()

    # Build a reverse map: placement_id → cell. Needed because the
    # evidence table stores placement IDs, not cells.
    pid_to_cell: dict[int, tuple[int, int, int]] = {
        pid: cell for cell, pid in cell_to_pid.items()
    }
    # Precompute neighbor counts per cell for speed — many edges share
    # the same endpoint cells so caching avoids recomputation.
    neighbor_cache: dict[tuple[int, int, int], int] = {}

    def _count(cell: tuple[int, int, int]) -> int:
        if cell in neighbor_cache:
            return neighbor_cache[cell]
        n = _count_non_drivable_neighbors(cell, cell_bucket)
        neighbor_cache[cell] = n
        return n

    updates: list[tuple[int, int, int, int, str]] = []
    flagged = 0
    for src_pid, dst_pid in edges:
        cell_a = pid_to_cell.get(int(src_pid))
        cell_b = pid_to_cell.get(int(dst_pid))
        if cell_a is None or cell_b is None:
            continue
        total = _count(cell_a) + _count(cell_b)
        if total >= threshold:
            updates.append((total, map_id, int(src_pid), int(dst_pid),
                            classification_version))
            flagged += 1

    if updates:
        with cursor(conn) as cur:
            cur.executemany(_UPDATE_NEGATIVE_EVIDENCE_SQL, updates)
        conn.commit()
    return len(edges), flagged


def update_negative_evidence(
    conn: Connection,
    map_ids: Iterable[int] | None = None,
    *,
    snapshot_id: str | None = None,
    classification_version: str = CLASSIFICATION_VERSION,
    limit: int | None = None,
    threshold: int = DECO_CLUSTER_NEIGHBOR_THRESHOLD,
) -> NegativeEvidenceStats:
    """Set-level Signal-4 updater. Per-map failures are captured."""
    stats = NegativeEvidenceStats(
        started_at=_utcnow(),
        classification_version=classification_version,
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
                examined, flagged = update_negative_evidence_for_map(
                    conn, mid,
                    classification_version=classification_version,
                    threshold=threshold,
                )
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                stats.errors.append(f"map={mid}: {exc}")
                _LOG.exception("negative_evidence update failed on map %d", mid)
                continue
            if flagged > 0:
                stats.maps_updated += 1
            stats.edges_examined += examined
            stats.edges_flagged += flagged
    finally:
        stats.completed_at = _utcnow()
    return stats


def update_path_support(
    conn: Connection,
    map_ids: Iterable[int] | None = None,
    *,
    snapshot_id: str | None = None,
    classification_version: str = CLASSIFICATION_VERSION,
    limit: int | None = None,
) -> PathSupportStats:
    """Run update_path_support_for_map across a map set. Per-map
    failures are captured, not fatal — mirrors build_set_evidence.
    """
    stats = PathSupportStats(
        started_at=_utcnow(),
        classification_version=classification_version,
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
                ivs, edges, paths = update_path_support_for_map(
                    conn, mid, classification_version=classification_version,
                )
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                stats.errors.append(f"map={mid}: {exc}")
                _LOG.exception("path_support update failed on map %d", mid)
                continue
            if edges == 0:
                stats.maps_skipped_no_evidence += 1
                continue
            stats.maps_updated += 1
            stats.intervals_enumerated += ivs
            stats.edges_updated += edges
            stats.path_count_total += paths
    finally:
        stats.completed_at = _utcnow()
    return stats


def build_set_evidence(
    conn: Connection,
    map_ids: Iterable[int] | None = None,
    *,
    snapshot_id: str | None = None,
    classification_version: str = CLASSIFICATION_VERSION,
    limit: int | None = None,
    batch_size: int = _INSERT_BATCH_SIZE,
) -> EvidenceBuildStats:
    """Build + persist evidence for every map in ``map_ids`` (or every
    parsed map if None). Per-map failures don't abort the run —
    they're captured in ``stats.errors`` so a partial run is still
    observable.
    """
    stats = EvidenceBuildStats(
        started_at=_utcnow(),
        classification_version=classification_version,
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
                counts = build_map_evidence(
                    conn, mid,
                    classification_version=classification_version,
                    batch_size=batch_size,
                )
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                stats.errors.append(f"map={mid}: {exc}")
                _LOG.exception("evidence build failed on map %d", mid)
                continue
            edges_here = counts["seed_valid"] + counts["unsupported"] + counts["unknown"]
            if edges_here == 0:
                stats.maps_skipped_no_placements += 1
                continue
            stats.maps_written += 1
            stats.edges_written += edges_here
            stats.seed_valid += counts["seed_valid"]
            stats.unsupported += counts["unsupported"]
            stats.unknown += counts["unknown"]
    finally:
        stats.completed_at = _utcnow()
    return stats
