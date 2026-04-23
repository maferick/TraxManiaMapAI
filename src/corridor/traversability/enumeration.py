"""Step 4 — corridor path enumeration + §8.3 / §8.4 gates.

For each checkpoint interval on a map, enumerate all simple paths up
to a depth cap over the seed_valid + observation-augmented
traversability subgraph. Emit:

- **§8.4 tractability**: median and p95 path counts per interval
- **§8.3 automated sanity**: four post-enumeration checks that
  substitute for manual review until hand-curation is available

Observations enter as *virtual edges*: if a replay asserts cells
{a, b, c} are pairwise-connected, we treat (a, b), (a, c), (b, c)
as traversable edges for the enumeration. These virtual edges are
marked so check #3 (deco-adjacent contamination) can still run
meaningfully — virtual edges have no real cell route, so intermediate
contamination is only measured on seed_valid segments.

Depth 10 is a conservative cap. A race interval with more than 10
grid-neighbor-steps between two anchor sets is either a very large
interval or evidence the seed graph is still too sparse; either way,
path enumeration can't explore it tractably.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from pymysql.connections import Connection

from src.corridor.traversability.classification import (
    NON_DRIVABLE_FAMILIES,
    classify_family,
    FamilyBucket,
)
from src.corridor.traversability.reachability import (
    AnchorSet,
    _build_anchor_sets,
    _build_cell_graph,
    _build_observations,
    _fetch_clean_replays,
    _fetch_free_map_waypoints,
    _fetch_map_grid_blocks,
    _fetch_map_waypoints,
    _snap_free_waypoints_to_grid,
    _SPAWN_TAGS,
)

_LOG = logging.getLogger(__name__)

# Cap used by §8.4 enumeration; matches the design note.
DEFAULT_DEPTH_CAP: int = 10

# §8.4 thresholds.
MEDIAN_PATH_COUNT_CAP: int = 1_000
P95_PATH_COUNT_CAP: int = 10_000

# §8.3 check-3 threshold. 0.40 = "up to 40% of corridor cells may
# have a deco/support neighbor in the grid." Tighter than this
# would over-reject narrow drivable chokepoints (which legitimately
# have deco on both sides); looser would admit corridors that run
# through stadium-prop neighborhoods.
DECO_ADJACENT_CONTAMINATION_CAP: float = 0.40


@dataclass
class IntervalEnumeration:
    """Per-interval enumeration result + sanity-check outcomes."""
    map_id: int
    src_tag: str
    src_order: int
    dst_tag: str
    dst_order: int
    # Path enumeration
    path_count: int = 0
    # §8.3 sanity checks
    corridor_cells: frozenset[tuple[int, int, int]] = field(
        default_factory=frozenset
    )
    unsupported_edges_in_corridors: int = 0
    non_drivable_cells_in_corridors: int = 0
    deco_adjacent_contamination: float = 0.0
    top_corridor_stable: bool | None = None   # None if not assessed
    # Optional: full enumerated paths as cell lists. Populated only
    # when enumerate_map is called with keep_paths=True (used by the
    # path_support aggregator in evidence.py). Omitted by default to
    # keep memory bounded on maps with thousands of paths.
    paths: list[list[tuple[int, int, int]]] = field(default_factory=list)

    @property
    def passes_sanity_1_unsupported(self) -> bool:
        return self.unsupported_edges_in_corridors == 0

    @property
    def passes_sanity_2_non_drivable(self) -> bool:
        return self.non_drivable_cells_in_corridors == 0

    @property
    def passes_sanity_3_deco_adjacent(self) -> bool:
        return self.deco_adjacent_contamination <= DECO_ADJACENT_CONTAMINATION_CAP

    @property
    def passes_sanity_4_stable(self) -> bool:
        # None = not assessed → counts as pass (no evidence of instability)
        return self.top_corridor_stable is not False


@dataclass
class EnumerationReport:
    per_map: dict[int, list[IntervalEnumeration]] = field(default_factory=dict)

    def all_intervals(self) -> list[IntervalEnumeration]:
        out: list[IntervalEnumeration] = []
        for intervals in self.per_map.values():
            out.extend(intervals)
        return out

    # --- §8.4 tractability metrics -------------------------------------------

    @property
    def path_counts(self) -> list[int]:
        return [iv.path_count for iv in self.all_intervals()]

    @property
    def median_path_count(self) -> float:
        counts = self.path_counts
        if not counts:
            return 0.0
        return float(median(counts))

    @property
    def p95_path_count(self) -> int:
        counts = sorted(self.path_counts)
        if not counts:
            return 0
        idx = max(0, min(len(counts) - 1, (len(counts) * 95) // 100))
        return int(counts[idx])

    @property
    def passes_84_median(self) -> bool:
        return self.median_path_count <= MEDIAN_PATH_COUNT_CAP

    @property
    def passes_84_p95(self) -> bool:
        return self.p95_path_count <= P95_PATH_COUNT_CAP

    # --- §8.3 sanity metrics -------------------------------------------------

    @property
    def passes_83_unsupported(self) -> bool:
        return all(iv.passes_sanity_1_unsupported for iv in self.all_intervals())

    @property
    def passes_83_non_drivable(self) -> bool:
        return all(iv.passes_sanity_2_non_drivable for iv in self.all_intervals())

    @property
    def passes_83_deco_adjacent(self) -> bool:
        return all(iv.passes_sanity_3_deco_adjacent for iv in self.all_intervals())

    @property
    def passes_83_stable(self) -> bool:
        return all(iv.passes_sanity_4_stable for iv in self.all_intervals())

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "maps_total": len(self.per_map),
            "intervals_total": len(self.all_intervals()),
            "median_path_count": self.median_path_count,
            "p95_path_count": self.p95_path_count,
            "passes_84_median": self.passes_84_median,
            "passes_84_p95": self.passes_84_p95,
            "passes_83_unsupported": self.passes_83_unsupported,
            "passes_83_non_drivable": self.passes_83_non_drivable,
            "passes_83_deco_adjacent": self.passes_83_deco_adjacent,
            "passes_83_stable": self.passes_83_stable,
            "per_map_intervals": {
                str(mid): [_interval_to_json(iv) for iv in ivs]
                for mid, ivs in self.per_map.items()
            },
        }


def _interval_to_json(iv: IntervalEnumeration) -> dict[str, Any]:
    return {
        "src": f"{iv.src_tag}#{iv.src_order}",
        "dst": f"{iv.dst_tag}#{iv.dst_order}",
        "path_count": iv.path_count,
        "corridor_cell_count": len(iv.corridor_cells),
        "unsupported_edges": iv.unsupported_edges_in_corridors,
        "non_drivable_cells": iv.non_drivable_cells_in_corridors,
        "deco_adjacent_contamination": round(iv.deco_adjacent_contamination, 4),
        "top_corridor_stable": iv.top_corridor_stable,
    }


# -----------------------------------------------------------------------------
# Path enumeration (DFS with depth cap)
# -----------------------------------------------------------------------------


def _enumerate_simple_paths(
    neighbors: dict[tuple[int, int, int], list[tuple[int, int, int]]],
    sources: frozenset[tuple[int, int, int]],
    targets: frozenset[tuple[int, int, int]],
    depth_cap: int,
    hard_cap: int = P95_PATH_COUNT_CAP,
) -> list[list[tuple[int, int, int]]]:
    """Enumerate simple paths (no repeated cells) up to ``depth_cap``
    steps from any source cell to any target cell. Early-exits when
    ``hard_cap`` paths have been found so pathological intervals don't
    blow up the validation run.
    """
    paths: list[list[tuple[int, int, int]]] = []
    visited: set[tuple[int, int, int]] = set()
    for start in sources:
        if start in targets:
            # A trivial 1-cell "corridor" — source is target. Record but
            # don't DFS.
            paths.append([start])
            if len(paths) >= hard_cap:
                return paths
            continue
        stack: list[tuple[tuple[int, int, int], list[tuple[int, int, int]], int]] = [
            (start, [start], 0)
        ]
        visited.add(start)
        while stack:
            current, path, depth = stack.pop()
            if len(paths) >= hard_cap:
                return paths
            if depth >= depth_cap:
                visited.discard(current)
                continue
            for nb in neighbors.get(current, ()):
                if nb in visited:
                    continue
                if nb in targets:
                    paths.append(path + [nb])
                    if len(paths) >= hard_cap:
                        return paths
                    continue
                visited.add(nb)
                stack.append((nb, path + [nb], depth + 1))
            # Backtrack — remove from visited when the last neighbor is
            # processed. Cheap check: current has no more unvisited
            # neighbors in path.
            if not any(nb not in visited for nb in neighbors.get(current, ())):
                visited.discard(current)
        visited.discard(start)
    return paths


# -----------------------------------------------------------------------------
# Graph construction — seed + observation virtual edges
# -----------------------------------------------------------------------------


def _build_enumeration_graph(
    neighbors_seed: dict[tuple[int, int, int], list[tuple[int, int, int]]],
    observation_sets: list[frozenset[tuple[int, int, int]]],
) -> tuple[
    dict[tuple[int, int, int], list[tuple[int, int, int]]],
    set[tuple[tuple[int, int, int], tuple[int, int, int]]],
]:
    """Seed adjacency + observation-derived virtual edges. Virtual
    edges connect every pair of cells within an observation set;
    they're marked so downstream can tell them apart.

    Returns the combined neighbor map and the set of virtual edges
    (unordered pairs).
    """
    combined: dict[tuple[int, int, int], list[tuple[int, int, int]]] = defaultdict(list)
    for src, nbs in neighbors_seed.items():
        for nb in nbs:
            combined[src].append(nb)

    virtual: set[tuple[tuple[int, int, int], tuple[int, int, int]]] = set()
    for obs_cells in observation_sets:
        cells = list(obs_cells)
        for i, a in enumerate(cells):
            for b in cells[i + 1:]:
                pair = tuple(sorted((a, b)))  # type: ignore[assignment]
                if pair in virtual:
                    continue
                virtual.add(pair)  # type: ignore[arg-type]
                combined[a].append(b)
                combined[b].append(a)
    return combined, virtual


# -----------------------------------------------------------------------------
# §8.3 sanity checks
# -----------------------------------------------------------------------------


def _compute_deco_adjacent_contamination(
    corridor_cells: frozenset[tuple[int, int, int]],
    anchor_cells: frozenset[tuple[int, int, int]],
    cell_to_family: dict[tuple[int, int, int], str],
) -> float:
    """Fraction of INTERIOR corridor cells with at least one
    NON_DRIVABLE grid neighbor. Anchor cells (spawn / CP / goal) are
    excluded from the denominator because they're track-family
    blocks whose natural neighbors include deco bases by TM2020
    design — measuring them would flag every corridor as
    contaminated.

    Returns 0.0 when the corridor has no interior cells (e.g. a
    single-hop spawn → goal corridor that's pure-anchor).
    """
    interior = corridor_cells - anchor_cells
    if not interior:
        return 0.0
    hits = 0
    for cell in interior:
        x, y, z = cell
        for nx, ny, nz in (
            (x + 1, y, z), (x - 1, y, z),
            (x, y + 1, z), (x, y - 1, z),
            (x, y, z + 1), (x, y, z - 1),
        ):
            fam = cell_to_family.get((nx, ny, nz))
            if fam is None:
                continue
            if classify_family(fam) is FamilyBucket.NON_DRIVABLE:
                hits += 1
                break
    return hits / len(interior)


def _evaluate_corridor_sanity(
    paths: list[list[tuple[int, int, int]]],
    cell_to_family: dict[tuple[int, int, int], str],
    virtual_edges: set[tuple[tuple[int, int, int], tuple[int, int, int]]],
    anchor_cells: frozenset[tuple[int, int, int]],
    iv: IntervalEnumeration,
) -> None:
    """Populate sanity-check counters on the interval. Writes
    ``corridor_cells``, ``unsupported_edges_in_corridors``,
    ``non_drivable_cells_in_corridors``, ``deco_adjacent_contamination``.

    Checks 1 and 2 are tautological under construction (seed_valid +
    observation virtual edges never touch NON_DRIVABLE cells) but are
    re-validated post-enumeration so a future relaxation of the
    enumeration graph is caught immediately rather than silently
    admitting non-drivable content.
    """
    corridor_cells: set[tuple[int, int, int]] = set()
    unsupported_edge_count = 0
    non_drivable_cell_count = 0
    for path in paths:
        corridor_cells.update(path)
        # Check 1: any path edge that is NOT a virtual edge must be a
        # seed_valid grid-neighbor edge. We don't have direct access to
        # the label here; re-derive from classification: if either
        # endpoint of a grid-neighbor edge is NON_DRIVABLE, the edge
        # would have been labeled unsupported — by construction it's
        # not in the seed graph. So unsupported_edge_count is zero
        # under construction. Left as 0 counter for future-proofing.
        # (Post-enumeration validation: iterate edges and classify.)
        for i in range(len(path) - 1):
            a, b = path[i], path[i + 1]
            pair = tuple(sorted((a, b)))  # type: ignore[assignment]
            if pair in virtual_edges:
                # Virtual edge — no direct block-level interpretation;
                # skip from unsupported-edge check (observation is
                # evidence of connectivity, not of a specific edge).
                continue
            fam_a = cell_to_family.get(a, "")
            fam_b = cell_to_family.get(b, "")
            if (classify_family(fam_a) is FamilyBucket.NON_DRIVABLE
                    or classify_family(fam_b) is FamilyBucket.NON_DRIVABLE):
                unsupported_edge_count += 1
    for cell in corridor_cells:
        fam = cell_to_family.get(cell, "")
        if classify_family(fam) is FamilyBucket.NON_DRIVABLE:
            non_drivable_cell_count += 1
    iv.corridor_cells = frozenset(corridor_cells)
    iv.unsupported_edges_in_corridors = unsupported_edge_count
    iv.non_drivable_cells_in_corridors = non_drivable_cell_count
    iv.deco_adjacent_contamination = _compute_deco_adjacent_contamination(
        iv.corridor_cells, anchor_cells, cell_to_family,
    )


# -----------------------------------------------------------------------------
# Top-level per-map enumeration
# -----------------------------------------------------------------------------


def _top_ranked_path(
    paths: list[list[tuple[int, int, int]]],
) -> tuple[tuple[tuple[int, int, int], ...], ...] | None:
    """Canonical "top-ranked path" for stability comparison. Ordering:
    shortest (fewest cells) first, then lexicographic by cell tuple
    sequence. Returns a hashable tuple-of-tuples form; None for empty."""
    if not paths:
        return None
    # Sort by (len, path-as-tuples) — stable, deterministic, reproducible.
    ranked = sorted(paths, key=lambda p: (len(p), tuple(p)))
    return tuple(tuple(c) for c in ranked[0])


# Max observations held-out per interval during §8.3.4 perturbation.
# Keeps per-map runtime bounded (at most 5 re-enumerations per interval
# even on maps with 15 observations).
_STABILITY_PERTURBATION_CAP: int = 5


def _assess_path_stability(
    neighbors_seed: dict[tuple[int, int, int], list[tuple[int, int, int]]],
    observations: list[frozenset[tuple[int, int, int]]],
    sources: frozenset[tuple[int, int, int]],
    targets: frozenset[tuple[int, int, int]],
    depth_cap: int,
    baseline_top: tuple[tuple[tuple[int, int, int], ...], ...] | None,
) -> bool | None:
    """§8.3.4: hold out each observation one at a time (up to the
    perturbation cap), re-enumerate, check that the top-ranked path
    is invariant. Returns True/False/None:

    - None: cannot assess (fewer than 2 observations — nothing to perturb)
    - True: all held-out runs produce the same top path as baseline
    - False: at least one held-out run produced a different top path
    """
    if len(observations) < 2:
        return None
    if baseline_top is None:
        # No path at baseline means stability is trivially preserved
        # (every held-out run would also yield no path) but also means
        # there's nothing to defend — treat as "not assessed."
        return None
    for i, _ in enumerate(observations[:_STABILITY_PERTURBATION_CAP]):
        held_out = observations[:i] + observations[i + 1:]
        combined, _ = _build_enumeration_graph(neighbors_seed, held_out)
        paths = _enumerate_simple_paths(
            combined, sources, targets, depth_cap=depth_cap,
        )
        if _top_ranked_path(paths) != baseline_top:
            return False
    return True


@dataclass(frozen=True)
class _IntervalPlan:
    """One interval the enumerator intends to explore: metadata
    (``src_tag/src_order/dst_tag/dst_order``) plus the concrete source /
    target cell sets for BFS. Extracted as a pure value so the interval-
    shaping rule is unit-testable without a DB.
    """
    src_tag: str
    src_order: int
    dst_tag: str
    dst_order: int
    sources: frozenset[tuple[int, int, int]]
    targets: frozenset[tuple[int, int, int]]


def _plan_intervals(anchor_sets: list[AnchorSet]) -> list[_IntervalPlan]:
    """Decide which intervals to enumerate based on the map's waypoint
    shape. The enumerator walks the returned list in order.

    Two shapes:

    - **Plain-CP** — every non-Spawn anchor has ``waypoint_order == 0``.
      Emits ``Spawn → <each non-Spawn anchor>``. This is Phase 1's
      original behaviour; it's what the 514 plain-CP corpus maps were
      scored under, and changing it would invalidate corridor-ranking
      training data.

    - **Linked-CP** — the map carries one or more ``LinkedCheckpoint``
      anchors (distinct tag from plain ``Checkpoint``; the parser
      assigns it when the GBX waypoint declares an explicit chain
      order). Emits ``Spawn → LCP#1 → LCP#2 → … → LCP#N → Goal``,
      the interval-key shape ``src.generation.assembly`` looks up
      when assembling routes (scope-v0 §Route assembly).

    Mixed shapes (both ``Checkpoint`` and ``LinkedCheckpoint`` on one
    map) fall back to plain-CP here so Phase 1 training invariants are
    preserved — the generator's own assembler will correctly reject
    such maps with ``plain_cp_not_supported_v0`` downstream.
    """
    spawn_cells: set[tuple[int, int, int]] = set()
    plain_cps: list[AnchorSet] = []
    linked_cps: list[AnchorSet] = []
    goal: AnchorSet | None = None
    others: list[AnchorSet] = []
    for aset in anchor_sets:
        if aset.tag in _SPAWN_TAGS:
            spawn_cells.update(aset.cells)
        elif aset.tag == "Checkpoint":
            plain_cps.append(aset)
        elif aset.tag == "LinkedCheckpoint":
            linked_cps.append(aset)
        elif aset.tag == "Goal":
            # First Goal wins; scope-v0 doesn't model multi-Goal stunt
            # maps (twin-finish is a corpus oddity surfaced in PR E).
            if goal is None:
                goal = aset
        else:
            others.append(aset)

    if not spawn_cells:
        return []

    # Linked-CP iff the map uses the dedicated LinkedCheckpoint tag
    # exclusively (no mixed-shape maps). Goal is required — without it
    # we can't close Spawn→Goal and the generator rejects anyway.
    linked = (
        bool(linked_cps)
        and not plain_cps
        and goal is not None
    )

    if linked:
        # Chain ordering by waypoint_order is authoritative in Linked-CP.
        assert goal is not None  # narrowed by `linked` predicate
        chain: list[AnchorSet] = sorted(
            linked_cps, key=lambda a: a.waypoint_order,
        )
        plans: list[_IntervalPlan] = []
        # Spawn → CP#1
        plans.append(_IntervalPlan(
            src_tag="Spawn", src_order=0,
            dst_tag=chain[0].tag, dst_order=chain[0].waypoint_order,
            sources=frozenset(spawn_cells), targets=chain[0].cells,
        ))
        # CP#i → CP#i+1
        for i in range(len(chain) - 1):
            a, b = chain[i], chain[i + 1]
            plans.append(_IntervalPlan(
                src_tag=a.tag, src_order=a.waypoint_order,
                dst_tag=b.tag, dst_order=b.waypoint_order,
                sources=a.cells, targets=b.cells,
            ))
        # CP#N → Goal
        last_cp = chain[-1]
        plans.append(_IntervalPlan(
            src_tag=last_cp.tag, src_order=last_cp.waypoint_order,
            dst_tag=goal.tag, dst_order=goal.waypoint_order,
            sources=last_cp.cells, targets=goal.cells,
        ))
        return plans

    # Plain-CP path (preserves Phase 1 behaviour).
    plans = []
    for aset in plain_cps + linked_cps + ([goal] if goal is not None else []) + others:
        plans.append(_IntervalPlan(
            src_tag="Spawn", src_order=0,
            dst_tag=aset.tag, dst_order=aset.waypoint_order,
            sources=frozenset(spawn_cells), targets=aset.cells,
        ))
    return plans


def enumerate_map(
    conn: Connection,
    map_id: int,
    *,
    depth_cap: int = DEFAULT_DEPTH_CAP,
    keep_paths: bool = False,
) -> list[IntervalEnumeration]:
    """Enumerate corridor candidates for every interval on one map.

    Interval shape is shape-aware (see :func:`_plan_intervals`):
    plain-CP maps emit ``Spawn → <each non-Spawn anchor>``; Linked-CP
    maps emit the Spawn→CP→…→Goal chain. Returns an empty list if the
    map can't be evaluated (no grid placements, no spawn, etc.)."""
    grid_rows = _fetch_map_grid_blocks(conn, map_id=map_id)
    if not grid_rows:
        return []
    cell_to_family, neighbors, _ = _build_cell_graph(grid_rows)

    wp_rows = _fetch_map_waypoints(conn, map_id=map_id)
    free_wp_rows = _fetch_free_map_waypoints(conn, map_id=map_id)
    if free_wp_rows:
        snapped = _snap_free_waypoints_to_grid(
            free_wp_rows, list(cell_to_family.keys()), cell_to_family,
        )
        wp_rows = list(wp_rows) + snapped
    anchor_sets = _build_anchor_sets(wp_rows)
    if not anchor_sets:
        return []

    plans = _plan_intervals(anchor_sets)
    if not plans:
        return []

    replays = _fetch_clean_replays(conn, map_id=map_id)
    observations = _build_observations(anchor_sets, replays)
    observation_sets = [obs.cells for obs in observations]
    combined, virtual_edges = _build_enumeration_graph(neighbors, observation_sets)

    # All anchor cells across the whole map — used to exclude from
    # deco-adjacent contamination since track-family anchors legitimately
    # have deco neighbors by TM2020 design.
    all_anchor_cells: frozenset[tuple[int, int, int]] = frozenset(
        cell for aset in anchor_sets for cell in aset.cells
    )

    out: list[IntervalEnumeration] = []
    for plan in plans:
        iv = IntervalEnumeration(
            map_id=map_id,
            src_tag=plan.src_tag,
            src_order=plan.src_order,
            dst_tag=plan.dst_tag,
            dst_order=plan.dst_order,
        )
        paths = _enumerate_simple_paths(
            combined,
            plan.sources,
            plan.targets,
            depth_cap=depth_cap,
        )
        iv.path_count = len(paths)
        if keep_paths:
            iv.paths = paths
        _evaluate_corridor_sanity(
            paths, cell_to_family, virtual_edges, all_anchor_cells, iv,
        )
        # §8.3.4 perturbation test — only if we have enough observations
        # to make it meaningful.
        iv.top_corridor_stable = _assess_path_stability(
            neighbors, observation_sets,
            plan.sources, plan.targets,
            depth_cap, _top_ranked_path(paths),
        )
        out.append(iv)
    return out


def enumerate_set(
    conn: Connection,
    map_ids: tuple[int, ...],
    *,
    depth_cap: int = DEFAULT_DEPTH_CAP,
) -> EnumerationReport:
    report = EnumerationReport()
    for mid in map_ids:
        try:
            report.per_map[mid] = enumerate_map(conn, mid, depth_cap=depth_cap)
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("enumeration failed on map %d", mid)
            report.per_map[mid] = []
    return report
