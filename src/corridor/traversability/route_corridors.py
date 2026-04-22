"""Persist enumerated corridor paths to the ``route_corridors`` table.

For each map, for each spawn → non-spawn interval, enumerate simple
paths (same DFS as enumeration.py) and write the top-N ranked paths
as rows. ``path_rank`` = 0 is the top-ranked path (shortest-first,
lex tiebreak — matches ``_top_ranked_path`` so the §8.3.4 stability
check and this persistence order use the same definition).

Consumers of this table:

- future corridor-ranking / scoring code that needs canonical
  candidate paths to score
- the PR 7 dry-run evaluator family that will surface
  corridor-confidence once Signal-aware ranking exists
- future OpenPlanet telemetry matching: per-tick position streams
  align to corridor candidates

Today it's the canonical storage for what ``enumerate_map`` produces
on-the-fly — persisted so consumers don't re-enumerate every time.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from pymysql.connections import Connection

from src.corridor.traversability.classification import CLASSIFICATION_VERSION
from src.corridor.traversability.evidence import _fetch_candidate_map_ids
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)

# Default top-N paths retained per interval. A top-100 rank window
# gives consumers the best candidates without blowing up storage on
# maps where enumeration hits the §8.4 10,000-path hard cap.
DEFAULT_TOP_N: int = 100


@dataclass
class RouteCorridorsStats:
    """Counters from a build run."""
    started_at: datetime
    classification_version: str
    top_n: int
    maps_seen: int = 0
    maps_with_intervals: int = 0
    intervals_written: int = 0
    paths_written: int = 0
    errors: list[str] = field(default_factory=list)
    completed_at: datetime | None = None

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "classification_version": self.classification_version,
            "top_n": self.top_n,
            "maps_seen": self.maps_seen,
            "maps_with_intervals": self.maps_with_intervals,
            "intervals_written": self.intervals_written,
            "paths_written": self.paths_written,
            "error_count": len(self.errors),
        }


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


_DELETE_MAP_ROWS_SQL = (
    "DELETE FROM route_corridors "
    "WHERE map_id = %s AND classification_version = %s"
)

_INSERT_SQL = """
INSERT INTO route_corridors (
    map_id, src_tag, src_order, dst_tag, dst_order,
    path_rank, path_cells, path_length, contains_virtual_edge,
    classification_version
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _rank_paths(
    paths: list[list[tuple[int, int, int]]],
) -> list[list[tuple[int, int, int]]]:
    """Canonical path ordering — shortest first, ties broken
    lexicographically by cell tuple sequence. Matches
    ``_top_ranked_path`` in enumeration.py so rank 0 stored here is
    the same top path the §8.3.4 stability check compares against.
    """
    return sorted(paths, key=lambda p: (len(p), tuple(p)))


def _path_contains_virtual_edge(
    path: list[tuple[int, int, int]],
    virtual_edges: set[tuple[tuple[int, int, int], tuple[int, int, int]]],
) -> bool:
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        pair = tuple(sorted((a, b)))  # type: ignore[assignment]
        if pair in virtual_edges:
            return True
    return False


def build_route_corridors_for_map(
    conn: Connection,
    map_id: int,
    *,
    classification_version: str = CLASSIFICATION_VERSION,
    top_n: int = DEFAULT_TOP_N,
) -> tuple[int, int]:
    """Enumerate + persist corridors for one map. Returns
    (intervals_written, paths_written). Raises on DB errors."""
    # Local import avoids circular dependency (route_corridors imports
    # enumeration; enumeration doesn't import this module).
    from src.corridor.traversability.enumeration import enumerate_map
    from src.corridor.traversability.reachability import (
        _build_anchor_sets,
        _build_cell_graph,
        _build_observations,
        _fetch_clean_replays,
        _fetch_free_map_waypoints,
        _fetch_map_grid_blocks,
        _fetch_map_waypoints,
        _snap_free_waypoints_to_grid,
    )
    from src.corridor.traversability.enumeration import _build_enumeration_graph

    intervals = enumerate_map(conn, map_id, keep_paths=True)
    if not intervals:
        return 0, 0

    # We need virtual_edges for the contains_virtual_edge flag.
    # Re-build the graph structures to get them. This is the only
    # duplication; the enumeration itself ran once inside
    # enumerate_map above.
    grid_rows = _fetch_map_grid_blocks(conn, map_id=map_id)
    cell_to_family, neighbors, _ = _build_cell_graph(grid_rows)
    wp_rows = _fetch_map_waypoints(conn, map_id=map_id)
    free_wp_rows = _fetch_free_map_waypoints(conn, map_id=map_id)
    if free_wp_rows:
        wp_rows = list(wp_rows) + _snap_free_waypoints_to_grid(
            free_wp_rows, list(cell_to_family.keys()), cell_to_family,
        )
    anchor_sets = _build_anchor_sets(wp_rows)
    replays = _fetch_clean_replays(conn, map_id=map_id)
    observations = _build_observations(anchor_sets, replays)
    _, virtual_edges = _build_enumeration_graph(
        neighbors, [obs.cells for obs in observations],
    )

    rows: list[tuple[Any, ...]] = []
    for iv in intervals:
        if not iv.paths:
            continue
        ranked = _rank_paths(iv.paths)
        if top_n is not None and top_n > 0:
            ranked = ranked[:top_n]
        for rank, path in enumerate(ranked):
            has_virtual = _path_contains_virtual_edge(path, virtual_edges)
            rows.append((
                map_id,
                iv.src_tag, iv.src_order,
                iv.dst_tag, iv.dst_order,
                rank,
                json.dumps([list(c) for c in path], separators=(",", ":")),
                len(path),
                int(has_virtual),
                classification_version,
            ))

    if not rows:
        return 0, 0

    with cursor(conn) as cur:
        cur.execute(_DELETE_MAP_ROWS_SQL, (map_id, classification_version))
        cur.executemany(_INSERT_SQL, rows)
    conn.commit()
    intervals_with_paths = sum(1 for iv in intervals if iv.paths)
    return intervals_with_paths, len(rows)


def build_route_corridors(
    conn: Connection,
    map_ids: Iterable[int] | None = None,
    *,
    snapshot_id: str | None = None,
    classification_version: str = CLASSIFICATION_VERSION,
    limit: int | None = None,
    top_n: int = DEFAULT_TOP_N,
) -> RouteCorridorsStats:
    """Set-level build. Per-map failures captured in stats.errors."""
    stats = RouteCorridorsStats(
        started_at=_utcnow(),
        classification_version=classification_version,
        top_n=top_n,
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
                intervals, paths = build_route_corridors_for_map(
                    conn, mid,
                    classification_version=classification_version,
                    top_n=top_n,
                )
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                stats.errors.append(f"map={mid}: {exc}")
                _LOG.exception("route_corridors build failed on map %d", mid)
                continue
            if intervals == 0:
                continue
            stats.maps_with_intervals += 1
            stats.intervals_written += intervals
            stats.paths_written += paths
    finally:
        stats.completed_at = _utcnow()
    return stats
