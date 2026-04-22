"""Per-map reachability validation over the traversability subgraph.

Implements Step 3 of the traversability prereq, per
``docs/workstreams/corridor-prereq-2-traversability.md`` §7 / §8.
Works directly on per-map ``block_placements`` — does NOT query Neo4j
— so the validation survives changes to the aggregate adjacency graph.

For each map in :data:`VALIDATION_MAP_IDS`:

1. Build a per-cell grid: ``(x, y, z) → block_family``.
2. Enumerate axis-neighbor edges within that grid.
3. Label each edge via :func:`label_edge` (seed_valid / unsupported /
   unknown). Keep only ``seed_valid`` edges as the map's traversability
   subgraph.
4. Group :class:`map_checkpoints` rows by ``(tag, waypoint_order)`` into
   **anchor sets** of cells. A single logical waypoint may span many
   cells (multi-cell ``GateExpandableFinish`` etc.); the anchor set
   preserves that multiplicity.
5. From the union of Spawn / StartFinish anchor cells, BFS over
   seed_valid edges. An anchor set is *reachable* if BFS touches any
   cell in the set. The map *passes* reachability if all non-Spawn
   anchor sets are reachable.

This is connectivity, not path enumeration — Step 4 does enumeration.
Step 3's job is to answer "given the classification, CAN the race be
completed at all on the traversability subgraph?" If not, Step 4 is
moot and we go back to classification.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from pymysql.connections import Connection

from src.corridor.traversability.labeling import (
    STATE_SEED_VALID,
    STATE_UNSUPPORTED,
    STATE_UNKNOWN,
    label_edge,
)
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


# Fixed 10-map validation set used by Phase-1 / Step-3 gating. Chosen
# from the 2026-04-scale-1k corpus to balance multilap / regular-CP
# structure and block-count diversity:
#
#   5 LinkedCheckpoint-heavy (multilap / ordered-sequence maps):
#     1212, 624, 912, 950, 403
#   5 regular-CP-heavy (plain Checkpoint race structure):
#     901, 676, 736, 418, 229
#
# Block counts span 176–19,524 grid blocks; waypoint counts span 5–78.
# The set is intentionally STABLE across runs — swapping members
# silently would undermine the Phase-1 gate.
VALIDATION_MAP_IDS: tuple[int, ...] = (
    1212, 624, 912, 950, 403,
    901, 676, 736, 418, 229,
)

# Waypoint tags that start a race (used as the BFS source set).
# ``StartFinish`` is a combined start+finish marker on some looping
# maps; if it's present, it's also treated as the spawn.
_SPAWN_TAGS: frozenset[str] = frozenset({"Spawn", "StartFinish"})


@dataclass(frozen=True)
class AnchorSet:
    """A logical waypoint represented as the set of cells it spans."""
    tag: str
    waypoint_order: int
    cells: frozenset[tuple[int, int, int]]


@dataclass
class MapReachability:
    """Per-map reachability + suppression numbers."""
    map_id: int
    total_cells: int = 0
    total_edges: int = 0
    seed_valid_edges: int = 0
    unsupported_edges: int = 0
    unknown_edges: int = 0
    anchor_sets_total: int = 0
    anchor_sets_reachable: int = 0
    spawn_anchor_cells: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def suppression_fraction(self) -> float:
        if self.total_edges == 0:
            return 0.0
        return (self.unsupported_edges + self.unknown_edges) / self.total_edges

    @property
    def unsupported_fraction(self) -> float:
        if self.total_edges == 0:
            return 0.0
        return self.unsupported_edges / self.total_edges

    @property
    def reachability_fraction(self) -> float:
        """Fraction of non-spawn anchor sets reachable from spawn. By
        convention 1.0 when there are no non-spawn anchors (trivially
        reachable).
        """
        non_spawn = self.anchor_sets_total
        if non_spawn == 0:
            return 1.0
        return self.anchor_sets_reachable / non_spawn

    @property
    def passes_reachability(self) -> bool:
        """Strict pass: every non-spawn anchor set reachable from spawn."""
        return self.reachability_fraction >= 1.0

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "map_id": self.map_id,
            "total_cells": self.total_cells,
            "total_edges": self.total_edges,
            "seed_valid_edges": self.seed_valid_edges,
            "unsupported_edges": self.unsupported_edges,
            "unknown_edges": self.unknown_edges,
            "anchor_sets_total": self.anchor_sets_total,
            "anchor_sets_reachable": self.anchor_sets_reachable,
            "spawn_anchor_cells": self.spawn_anchor_cells,
            "suppression_fraction": round(self.suppression_fraction, 4),
            "unsupported_fraction": round(self.unsupported_fraction, 4),
            "reachability_fraction": round(self.reachability_fraction, 4),
            "passes_reachability": self.passes_reachability,
            "error_count": len(self.errors),
        }


@dataclass
class ValidationReport:
    """Aggregate across the whole validation set. Field names align with
    the §8 commit-bar criteria so readers don't have to translate."""
    per_map: list[MapReachability] = field(default_factory=list)

    @property
    def maps_total(self) -> int:
        return len(self.per_map)

    @property
    def maps_passing_reachability(self) -> int:
        return sum(1 for m in self.per_map if m.passes_reachability)

    @property
    def intervals_total(self) -> int:
        return sum(m.anchor_sets_total for m in self.per_map)

    @property
    def intervals_reachable(self) -> int:
        return sum(m.anchor_sets_reachable for m in self.per_map)

    @property
    def interval_reachability_fraction(self) -> float:
        if self.intervals_total == 0:
            return 0.0
        return self.intervals_reachable / self.intervals_total

    @property
    def weighted_unsupported_fraction(self) -> float:
        total_edges = sum(m.total_edges for m in self.per_map)
        if total_edges == 0:
            return 0.0
        total_unsupported = sum(m.unsupported_edges for m in self.per_map)
        return total_unsupported / total_edges

    @property
    def weighted_suppression_fraction(self) -> float:
        total_edges = sum(m.total_edges for m in self.per_map)
        if total_edges == 0:
            return 0.0
        total_suppressed = sum(
            m.unsupported_edges + m.unknown_edges for m in self.per_map
        )
        return total_suppressed / total_edges

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "maps_total": self.maps_total,
            "maps_passing_reachability": self.maps_passing_reachability,
            "intervals_total": self.intervals_total,
            "intervals_reachable": self.intervals_reachable,
            "interval_reachability_fraction":
                round(self.interval_reachability_fraction, 4),
            "weighted_unsupported_fraction":
                round(self.weighted_unsupported_fraction, 4),
            "weighted_suppression_fraction":
                round(self.weighted_suppression_fraction, 4),
            "per_map": [m.to_summary_json() for m in self.per_map],
        }


# -----------------------------------------------------------------------------
# Graph construction
# -----------------------------------------------------------------------------


def _build_cell_graph(
    rows: list[tuple[int, int, int, str]]
) -> tuple[dict[tuple[int, int, int], str],
           dict[tuple[int, int, int], list[tuple[int, int, int]]],
           dict[str, int]]:
    """From a map's (x, y, z, family) rows, build:

    - ``cell_to_family``: ``{(x, y, z): family}`` (first placement wins
      per cell, mirroring the existing constraint extractor's
      first-wins policy)
    - ``seed_valid_neighbors``: ``{cell: [neighbor_cell, ...]}`` over
      seed_valid edges only
    - edge-count histogram across the three states

    Returns the neighbors dict as an undirected adjacency view (both
    directions added) to keep BFS symmetric.
    """
    cell_to_family: dict[tuple[int, int, int], str] = {}
    for x, y, z, family in rows:
        cell_to_family.setdefault((x, y, z), family)

    counts = {
        STATE_SEED_VALID: 0,
        STATE_UNSUPPORTED: 0,
        STATE_UNKNOWN: 0,
    }
    neighbors: dict[tuple[int, int, int], list[tuple[int, int, int]]] = defaultdict(list)
    seen_pairs: set[tuple[tuple[int, int, int], tuple[int, int, int]]] = set()
    for cell, family in cell_to_family.items():
        x, y, z = cell
        for nx, ny, nz in (
            (x + 1, y, z), (x - 1, y, z),
            (x, y + 1, z), (x, y - 1, z),
            (x, y, z + 1), (x, y, z - 1),
        ):
            nb = (nx, ny, nz)
            nb_family = cell_to_family.get(nb)
            if nb_family is None:
                continue
            key = tuple(sorted((cell, nb)))  # type: ignore[assignment]
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            label = label_edge(family, nb_family)
            counts[label.state] = counts.get(label.state, 0) + 1
            if label.state == STATE_SEED_VALID:
                neighbors[cell].append(nb)
                neighbors[nb].append(cell)
    return cell_to_family, neighbors, counts


# -----------------------------------------------------------------------------
# Anchor-set construction
# -----------------------------------------------------------------------------


def _build_anchor_sets(
    rows: list[tuple[str, int, int, int, int]]
) -> list[AnchorSet]:
    """Group ``map_checkpoints`` rows into logical waypoints. Rows are
    aggregated by ``(tag, waypoint_order)`` because a single multi-cell
    gate emits one row per occupied cell with identical tag+order.
    Rows whose placement is 'free' or whose x/y/z are NULL are skipped
    — those would need a spatial-snap step that's out of scope for
    Phase 1.
    """
    groups: dict[tuple[str, int], set[tuple[int, int, int]]] = defaultdict(set)
    for tag, wp_order, x, y, z in rows:
        if tag is None or x is None or y is None or z is None:
            continue
        groups[(tag, int(wp_order))].add((int(x), int(y), int(z)))
    return [
        AnchorSet(tag=tag, waypoint_order=wp_order, cells=frozenset(cells))
        for (tag, wp_order), cells in sorted(groups.items())
    ]


# -----------------------------------------------------------------------------
# Reachability
# -----------------------------------------------------------------------------


def _bfs_reachable(
    neighbors: dict[tuple[int, int, int], list[tuple[int, int, int]]],
    sources: frozenset[tuple[int, int, int]],
) -> set[tuple[int, int, int]]:
    """Plain BFS from any source cell over the seed_valid neighbor map.
    Returns the closed set of reachable cells (source cells are
    included). Handles source cells that aren't in ``neighbors`` (i.e.
    a spawn block with no seed_valid outgoing edge) — they're reachable
    only from themselves."""
    visited: set[tuple[int, int, int]] = set()
    queue: deque[tuple[int, int, int]] = deque()
    for src in sources:
        if src in visited:
            continue
        visited.add(src)
        queue.append(src)
    while queue:
        cur = queue.popleft()
        for nb in neighbors.get(cur, ()):
            if nb in visited:
                continue
            visited.add(nb)
            queue.append(nb)
    return visited


# -----------------------------------------------------------------------------
# DB queries
# -----------------------------------------------------------------------------


def _fetch_map_grid_blocks(
    conn: Connection, *, map_id: int
) -> list[tuple[int, int, int, str]]:
    with cursor(conn) as cur:
        cur.execute(
            "SELECT x, y, z, block_family FROM block_placements "
            "WHERE map_id = %s AND is_free = 0",
            (map_id,),
        )
        return [
            (int(r[0]), int(r[1]), int(r[2]), str(r[3] or ""))
            for r in cur.fetchall()
            if r[0] is not None and r[1] is not None and r[2] is not None
        ]


def _fetch_map_waypoints(
    conn: Connection, *, map_id: int
) -> list[tuple[str, int, int, int, int]]:
    with cursor(conn) as cur:
        cur.execute(
            "SELECT tag, waypoint_order, x, y, z "
            "FROM map_checkpoints "
            "WHERE map_id = %s AND placement = 'grid' "
            "AND x IS NOT NULL AND y IS NOT NULL AND z IS NOT NULL",
            (map_id,),
        )
        return list(cur.fetchall())


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------


def validate_map(conn: Connection, map_id: int) -> MapReachability:
    result = MapReachability(map_id=map_id)

    grid_rows = _fetch_map_grid_blocks(conn, map_id=map_id)
    if not grid_rows:
        result.errors.append("no_grid_placements")
        return result

    cell_to_family, neighbors, edge_counts = _build_cell_graph(grid_rows)
    result.total_cells = len(cell_to_family)
    result.seed_valid_edges = edge_counts[STATE_SEED_VALID]
    result.unsupported_edges = edge_counts[STATE_UNSUPPORTED]
    result.unknown_edges = edge_counts[STATE_UNKNOWN]
    result.total_edges = (
        result.seed_valid_edges + result.unsupported_edges + result.unknown_edges
    )

    wp_rows = _fetch_map_waypoints(conn, map_id=map_id)
    anchor_sets = _build_anchor_sets(wp_rows)
    if not anchor_sets:
        result.errors.append("no_grid_waypoints")
        return result

    spawn_cells: set[tuple[int, int, int]] = set()
    non_spawn_sets: list[AnchorSet] = []
    for aset in anchor_sets:
        if aset.tag in _SPAWN_TAGS:
            spawn_cells.update(aset.cells)
        else:
            non_spawn_sets.append(aset)
    result.spawn_anchor_cells = len(spawn_cells)
    result.anchor_sets_total = len(non_spawn_sets)

    if not spawn_cells:
        result.errors.append("no_spawn_anchor")
        return result
    if not non_spawn_sets:
        # Trivially "reachable" — nothing to reach. Rare but possible on
        # a map that has only a combined StartFinish waypoint.
        return result

    reachable = _bfs_reachable(neighbors, frozenset(spawn_cells))
    for aset in non_spawn_sets:
        if any(cell in reachable for cell in aset.cells):
            result.anchor_sets_reachable += 1
    return result


def validate_set(
    conn: Connection, map_ids: tuple[int, ...] = VALIDATION_MAP_IDS
) -> ValidationReport:
    report = ValidationReport()
    for mid in map_ids:
        try:
            report.per_map.append(validate_map(conn, mid))
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("validation failed on map %d", mid)
            report.per_map.append(
                MapReachability(map_id=mid, errors=[f"exception: {exc}"])
            )
    return report
