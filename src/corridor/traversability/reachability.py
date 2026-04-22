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

**Inductive reachability** (``use_observations=True``). Grid-adjacency
alone underestimates real TM2020 connectivity — tracks cross gaps via
ramps, loops, airborne sections. Replay breadcrumbs observe race-end
connectivity directly: a clean replay with finish_time_ms set proves
that spawn→goal IS traversable, regardless of whether the adjacency
graph captures the path. When observations are enabled, each clean
replay on the map contributes a connectivity assertion: spawn + all
observed checkpoints + goal are pairwise-connected. These assertions
are applied as union-find merges on top of the seed_valid BFS result.

The observations go INTO the traversability-reachability computation;
they do NOT bump constraint-graph validity (see
``src/constraints/evidence.py`` — "frequency is NOT validity"). Per
the design note §6.3, only explicit replay-crossing evidence on a
*specific edge* bumps ``replay_supported_count``. Observation-based
reachability is a different artifact — a connectivity assertion over
an anchor set, not an edge assertion.

This is connectivity, not path enumeration — Step 4 does enumeration.
Step 3's job is to answer "given the classification (optionally plus
replay-observed connectivity), CAN the race be completed at all on the
traversability subgraph?" If not, Step 4 is moot and we go back to
classification or to adjacency-model upgrades.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pymysql.connections import Connection

from src.corridor.traversability.classification import (
    FamilyBucket,
    classify_family,
)
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
VALIDATION_MAP_IDS_V1: tuple[int, ...] = (
    1212, 624, 912, 950, 403,
    901, 676, 736, 418, 229,
)
# V2: data-coverage-aware validation set. Selected from maps that have
# ≥3 clean breadcrumb replays in the 2026-04-scale-1k corpus so the
# inductive observation layer has something to work with. V1 stays
# frozen as the historical first-gate record; V2 is what the Phase 3
# mechanism is validated against going forward.
#
# Structural composition:
#   9 plain-Checkpoint maps (diverse block counts 93 to 7151)
#   1 LinkedCheckpoint map (1212 — only multilap map with ≥3 replays)
# Replay counts across V2: 3–15 per map.
VALIDATION_MAP_IDS_V2: tuple[int, ...] = (
    491, 1156, 298, 990, 1046,
    336, 803, 1177, 787, 1212,
)
# Current default — callers can pin to V1 explicitly for historical runs.
VALIDATION_MAP_IDS: tuple[int, ...] = VALIDATION_MAP_IDS_V2

# Approximate block dimensions in absolute coords. TM2020 grid cells
# are 32m × 8m × 32m (X × Y × Z); the center of the cell at grid
# coord (x, y, z) sits at approximately (x*32 + 16, y*8 + 4, z*32 + 16).
# Used only for snapping free-placed waypoint triggers to the nearest
# grid block; approximate because per-block-type anchor offsets vary
# slightly and we don't model them.
_BLOCK_SIZE_X: float = 32.0
_BLOCK_SIZE_Y: float = 8.0
_BLOCK_SIZE_Z: float = 32.0

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


@dataclass(frozen=True)
class ReplayObservation:
    """One clean replay's assertion that a set of anchor cells is
    pairwise-connected in the actual game. The cells come from the
    map's ``map_checkpoints`` rows filtered to waypoints the replay is
    known to have crossed (always spawn + goal when finish_time_ms is
    set; intermediate CPs included when the map's waypoint count
    matches the replay's ``checkpoint_times_ms`` length).
    """
    replay_id: int
    kind: str  # "spawn_goal_only" | "linked_ordered" | "checkpoint_matched"
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
    # Inductive fields — populated when observations are applied.
    observations_available: int = 0
    observations_applied: int = 0
    anchor_sets_reachable_seed_only: int = 0   # before observation merges
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
            "anchor_sets_reachable_seed_only": self.anchor_sets_reachable_seed_only,
            "observations_available": self.observations_available,
            "observations_applied": self.observations_applied,
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
    # Priority-first placement: when multiple blocks share a cell
    # (TM2020 allows layered placements — e.g. a checkpoint block on
    # top of a structural base), prefer the most-drivable family. A
    # non-drivable Structure block + a drivable RoadCheckpoint at the
    # same cell should resolve to Road, not Structure, because the
    # drivable one is what the car traverses. Tie-break by first
    # placement (mirrors the original first-wins semantics for the
    # common single-block case).
    cell_to_family: dict[tuple[int, int, int], str] = {}
    for x, y, z, family in rows:
        cell = (x, y, z)
        existing = cell_to_family.get(cell)
        if existing is None:
            cell_to_family[cell] = family
            continue
        # Promote only if the new family is a better bucket than the
        # existing one. Ordering: DRIVABLE > AMBIGUOUS > NON_DRIVABLE.
        existing_bucket = classify_family(existing)
        new_bucket = classify_family(family)
        if (existing_bucket is FamilyBucket.NON_DRIVABLE
                and new_bucket is not FamilyBucket.NON_DRIVABLE):
            cell_to_family[cell] = family
        elif (existing_bucket is FamilyBucket.AMBIGUOUS
              and new_bucket is FamilyBucket.DRIVABLE):
            cell_to_family[cell] = family

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
    """Grid-placed waypoints only; free-placed waypoints are fetched
    separately via :func:`_fetch_free_map_waypoints` so the caller can
    decide whether to snap them to the grid."""
    with cursor(conn) as cur:
        cur.execute(
            "SELECT tag, waypoint_order, x, y, z "
            "FROM map_checkpoints "
            "WHERE map_id = %s AND placement = 'grid' "
            "AND x IS NOT NULL AND y IS NOT NULL AND z IS NOT NULL",
            (map_id,),
        )
        return list(cur.fetchall())


def _fetch_free_map_waypoints(
    conn: Connection, *, map_id: int
) -> list[tuple[str, int, float, float, float]]:
    """Return ``(tag, waypoint_order, abs_x, abs_y, abs_z)`` for free-
    placed waypoints with populated absolute coords."""
    with cursor(conn) as cur:
        cur.execute(
            "SELECT tag, waypoint_order, abs_x, abs_y, abs_z "
            "FROM map_checkpoints "
            "WHERE map_id = %s AND placement = 'free' "
            "AND abs_x IS NOT NULL AND abs_y IS NOT NULL AND abs_z IS NOT NULL",
            (map_id,),
        )
        return [
            (str(r[0]), int(r[1]), float(r[2]), float(r[3]), float(r[4]))
            for r in cur.fetchall()
        ]


def _snap_free_waypoints_to_grid(
    free_waypoints: list[tuple[str, int, float, float, float]],
    grid_cells: list[tuple[int, int, int]],
    cell_to_family: dict[tuple[int, int, int], str] | None = None,
) -> list[tuple[str, int, int, int, int]]:
    """Snap free-placed waypoints to the nearest grid cell.

    When ``cell_to_family`` is supplied, prefer the nearest cell whose
    family is DRIVABLE. A waypoint block is always drivable by
    construction — it's a Road/Platform/Gate block the car crosses —
    so the nearest grid cell SHOULD be drivable. Snapping to the
    absolute-nearest cell without the preference can land on an
    adjacent Structure pillar or Deco base that happens to sit closer
    in absolute coords.

    Fallback: if no drivable cell is within the map (rare), snap to
    the absolute nearest cell — the caller's downstream classification
    will still flag the mismatch.

    Skip-silently policy: a free waypoint with zero grid blocks in
    the map produces nothing (the caller logs 'no_grid_waypoints'
    if that leaves the map without any anchors). Conservative to
    avoid inventing anchors where there's no block to snap to.
    """
    if not grid_cells:
        return []
    grid_abs: list[tuple[int, int, int, float, float, float]] = [
        (
            gx, gy, gz,
            gx * _BLOCK_SIZE_X + _BLOCK_SIZE_X / 2,
            gy * _BLOCK_SIZE_Y + _BLOCK_SIZE_Y / 2,
            gz * _BLOCK_SIZE_Z + _BLOCK_SIZE_Z / 2,
        )
        for gx, gy, gz in grid_cells
    ]
    snapped: list[tuple[str, int, int, int, int]] = []
    for tag, order, ax, ay, az in free_waypoints:
        best_drivable_dist = float("inf")
        best_drivable_cell: tuple[int, int, int] | None = None
        best_any_dist = float("inf")
        best_any_cell: tuple[int, int, int] | None = None
        for gx, gy, gz, cx, cy, cz in grid_abs:
            dx = ax - cx
            dy = ay - cy
            dz = az - cz
            dist = dx * dx + dy * dy + dz * dz
            if dist < best_any_dist:
                best_any_dist = dist
                best_any_cell = (gx, gy, gz)
            if cell_to_family is not None:
                fam = cell_to_family.get((gx, gy, gz), "")
                if classify_family(fam) is FamilyBucket.DRIVABLE:
                    if dist < best_drivable_dist:
                        best_drivable_dist = dist
                        best_drivable_cell = (gx, gy, gz)
        chosen = best_drivable_cell if best_drivable_cell is not None else best_any_cell
        if chosen is not None:
            snapped.append((tag, order, chosen[0], chosen[1], chosen[2]))
    return snapped


def _fetch_clean_replays(
    conn: Connection, *, map_id: int
) -> list[tuple[int, str]]:
    """Return ``(replay_id, breadcrumbs_path)`` for clean or
    usable-with-warnings replays that have a breadcrumb sidecar on disk."""
    with cursor(conn) as cur:
        cur.execute(
            "SELECT id, breadcrumbs_path FROM replays "
            "WHERE map_id = %s AND clean_status IN ('clean','usable_with_warnings') "
            "AND breadcrumbs_path IS NOT NULL",
            (map_id,),
        )
        return [(int(r[0]), str(r[1])) for r in cur.fetchall()]


def _load_breadcrumbs_cp_count(path: str) -> int | None:
    """Peek at a breadcrumbs sidecar to get the length of
    ``checkpoint_times_ms`` (number of timed checkpoint crossings,
    including the finish if present). Returns None if the file is
    missing or malformed — observations silently degrade to
    spawn-only when that happens."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cps = payload.get("checkpoint_times_ms")
    if not isinstance(cps, list):
        return None
    return len(cps)


def _build_observations(
    anchor_sets: list[AnchorSet],
    replays: list[tuple[int, str]],
) -> list[ReplayObservation]:
    """For each replay, classify what connectivity we can assert.

    The assertion is: *every cell in* ``cells`` *is pairwise-connected
    to every other cell in* ``cells`` *because this replay was driven
    cleanly*.

    Decision rules:

    - If the replay's ``checkpoint_times_ms`` length matches the number
      of intermediate-plus-finish anchor sets AND the map has
      ``LinkedCheckpoint`` or plain ``Checkpoint`` waypoints (not a mix
      that'd require heuristic disambiguation), include ALL waypoint
      cells (spawn + every CP + every goal cell).
    - Otherwise fall back to ``spawn_goal_only``: spawn cells + goal
      cells are the only asserted pairwise-connected anchors.
    """
    spawn_cells: set[tuple[int, int, int]] = set()
    goal_cells: set[tuple[int, int, int]] = set()
    linked_cells: set[tuple[int, int, int]] = set()
    checkpoint_cells: set[tuple[int, int, int]] = set()
    has_linked = False
    has_plain_cp = False
    for aset in anchor_sets:
        if aset.tag in _SPAWN_TAGS:
            spawn_cells.update(aset.cells)
        if aset.tag in ("Goal", "StartFinish"):
            goal_cells.update(aset.cells)
        if aset.tag == "LinkedCheckpoint":
            linked_cells.update(aset.cells)
            has_linked = True
        if aset.tag == "Checkpoint":
            checkpoint_cells.update(aset.cells)
            has_plain_cp = True

    # Count of distinct intermediate-CP logical anchors. LinkedCheckpoint
    # anchors each have a unique waypoint_order so the anchor_set count
    # equals the logical-CP count. Plain Checkpoint anchors all share
    # order=0 (TM2020 resolves order at run time from the player's
    # trajectory), so they collapse into a single anchor_set — the real
    # count we want to match against a replay's checkpoint_times_ms
    # length is the number of distinct Checkpoint *cells*.
    linked_anchor_count = sum(
        1 for a in anchor_sets if a.tag == "LinkedCheckpoint"
    )
    plain_cp_anchor_count = len(checkpoint_cells)

    out: list[ReplayObservation] = []
    for replay_id, bc_path in replays:
        cp_count = _load_breadcrumbs_cp_count(bc_path)
        # The sidecar's checkpoint_times_ms typically includes intermediate
        # CPs AND the finish — so a map with N intermediate CPs + a finish
        # gate matches replay cp_count == N + 1. Allow exact match or
        # match-off-by-one (some maps emit a StartFinish with no explicit
        # goal anchor, and the breadcrumb may omit the start time).
        asserted = spawn_cells | goal_cells
        kind = "spawn_goal_only"
        if cp_count is not None and not (has_linked and has_plain_cp):
            if has_linked and cp_count in (linked_anchor_count, linked_anchor_count + 1):
                asserted = asserted | linked_cells
                kind = "linked_ordered"
            elif has_plain_cp and cp_count in (plain_cp_anchor_count, plain_cp_anchor_count + 1):
                asserted = asserted | checkpoint_cells
                kind = "checkpoint_matched"
        if not asserted or len(asserted) < 2:
            # Nothing useful to assert — skip silently.
            continue
        out.append(ReplayObservation(
            replay_id=replay_id,
            kind=kind,
            cells=frozenset(asserted),
        ))
    return out


class _UnionFind:
    """Tiny union-find over arbitrary hashables. Path compression +
    union by size. Used to merge seed_valid edges and replay
    observations into a single connectivity relation per map."""

    def __init__(self) -> None:
        self._parent: dict[Any, Any] = {}
        self._size: dict[Any, int] = {}

    def _ensure(self, x: Any) -> None:
        if x not in self._parent:
            self._parent[x] = x
            self._size[x] = 1

    def find(self, x: Any) -> Any:
        self._ensure(x)
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._size[ra] < self._size[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        self._size[ra] += self._size[rb]

    def same(self, a: Any, b: Any) -> bool:
        return self.find(a) == self.find(b)


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------


def validate_map(
    conn: Connection,
    map_id: int,
    *,
    use_observations: bool = False,
) -> MapReachability:
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
    free_wp_rows = _fetch_free_map_waypoints(conn, map_id=map_id)
    if free_wp_rows:
        snapped = _snap_free_waypoints_to_grid(
            free_wp_rows, list(cell_to_family.keys()), cell_to_family
        )
        wp_rows = list(wp_rows) + snapped
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
        # Trivially "reachable" — nothing to reach.
        return result

    # Seed-only reachability always runs first; the seed-only count is
    # preserved for before/after comparison even when observations are
    # applied on top.
    seed_reachable = _bfs_reachable(neighbors, frozenset(spawn_cells))
    result.anchor_sets_reachable_seed_only = sum(
        1 for aset in non_spawn_sets
        if any(cell in seed_reachable for cell in aset.cells)
    )

    if not use_observations:
        result.anchor_sets_reachable = result.anchor_sets_reachable_seed_only
        return result

    # Inductive path: union-find the seed_valid neighbor relation with
    # replay observation sets, then reachability = "anchor cell in the
    # same component as any spawn cell."
    replays = _fetch_clean_replays(conn, map_id=map_id)
    observations = _build_observations(anchor_sets, replays)
    result.observations_available = len(replays)
    result.observations_applied = len(observations)

    uf: _UnionFind = _UnionFind()
    # Seed edges → union merges.
    for cell, nbs in neighbors.items():
        for nb in nbs:
            uf.union(cell, nb)
    # Observations → union every pair in the observed set with an anchor
    # cell from the set (chain-merging all of them into one component).
    for obs in observations:
        cells_iter = iter(obs.cells)
        try:
            head = next(cells_iter)
        except StopIteration:
            continue
        for cell in cells_iter:
            uf.union(head, cell)

    # Pick any spawn cell as the race's origin component representative.
    spawn_rep = uf.find(next(iter(spawn_cells)))
    # Union all spawn cells together (they may be in different components
    # before observations; after observations they're typically merged
    # via the spawn→goal assertion from any finishing replay).
    for sc in spawn_cells:
        uf.union(sc, spawn_rep)
    spawn_rep = uf.find(spawn_rep)

    reachable_count = 0
    for aset in non_spawn_sets:
        for cell in aset.cells:
            if uf.find(cell) == spawn_rep:
                reachable_count += 1
                break
    result.anchor_sets_reachable = reachable_count
    return result


def validate_set(
    conn: Connection,
    map_ids: tuple[int, ...] = VALIDATION_MAP_IDS,
    *,
    use_observations: bool = False,
) -> ValidationReport:
    report = ValidationReport()
    for mid in map_ids:
        try:
            report.per_map.append(
                validate_map(conn, mid, use_observations=use_observations)
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("validation failed on map %d", mid)
            report.per_map.append(
                MapReachability(map_id=mid, errors=[f"exception: {exc}"])
            )
    return report
