"""Route assembly per ``docs/generation/generation-scope-v0.md`` §
Route assembly.

Two entry points:

- :func:`assemble_route_from_inputs` — pure on already-fetched data
  (anchors + candidate corridors). Ideal for unit tests and for
  future callers that hold their inputs in memory (e.g. an
  in-process generator that just produced fresh corridors).
- :func:`assemble_route` — DB wrapper that materialises the inputs
  from MariaDB, then delegates to the pure function.

Both return ``AssembledRoute | AssemblyError`` — never a partial
route. The caller hands the result to
:func:`src.generation.finishability.run_finishability_gate` to get
the operator-facing verdict.

Tie-breaks + continuity checks are documented in the scope doc and
pinned here by the ``_TIE_BREAK_KEY`` + ``_cells_continuous``
helpers so reviewers can diff implementations against the spec.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Sequence

from pymysql.connections import Connection

# Import the same physics constants the label builder uses so
# estimated_time is consistent across label-time + gate-time. The
# scope doc explicitly calls this out ("Don't redefine these
# constants in the generator — import from the existing module.
# Keeps physics consistent between label-time and gate-time.").
from src.corridor.ranking.time_envelope_labels import (
    _BLOCK_SIZE_M,
    _DEFAULT_SPEED_PRIOR_M_S,
)
from src.corridor.traversability.classification import CLASSIFICATION_VERSION
from src.generation.types import (
    Anchor,
    AssembledRoute,
    AssemblyError,
    Cell,
    ChosenCorridor,
    IntervalAssembly,
)
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Inputs to the pure assembly pass — kept in one dataclass so the DB
# wrapper has a single object to build and the unit tests a single
# object to construct.
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class CandidateCorridor:
    """One row from route_corridors, shaped for assembly. Order within
    an interval is not meaningful; the algorithm sorts them."""
    corridor_id: int
    map_id: int
    src: Anchor
    dst: Anchor
    path_cells: tuple[Cell, ...]
    path_length: int
    contains_virtual_edge: bool
    corridor_confidence: float | None
    learned_corridor_score: float | None


@dataclass(frozen=True)
class AssemblyInputs:
    """Pure inputs for :func:`assemble_route_from_inputs`. Keeps the
    function signature small + testable without fake-DB plumbing."""
    map_id: int
    is_linked_cp: bool
    anchors: tuple[Anchor, ...]          # Spawn → CP₁ → … → Goal
    candidates: tuple[CandidateCorridor, ...]


# ---------------------------------------------------------------------
# Physics
# ---------------------------------------------------------------------

def _expected_time_ms(path_length_cells: int) -> int:
    """Per-corridor expected completion time, scope-v0 §Route assembly:
        expected_time_ms = path_length_cells * BLOCK_SIZE_M / SPEED_PRIOR_M_S * 1000
    Uses the same constants as the time_envelope label so label-time
    and gate-time don't drift."""
    if path_length_cells <= 0:
        return 0
    seconds = (path_length_cells * _BLOCK_SIZE_M) / _DEFAULT_SPEED_PRIOR_M_S
    return int(round(seconds * 1000.0))


# ---------------------------------------------------------------------
# Tie-break + continuity
# ---------------------------------------------------------------------

def _tie_break_key(c: CandidateCorridor) -> tuple:
    """Sort key used to pick the top candidate per interval.

    Primary: highest ``learned_corridor_score`` (negate for ascending
    sort). Tie-breakers match scope-v0 exactly:
        1) shorter ``path_length``
        2) lower ``corridor_id``

    The scope doc pins this ordering so different implementations
    don't drift on near-tie cases."""
    return (
        -(c.learned_corridor_score if c.learned_corridor_score is not None else -1.0),
        c.path_length,
        c.corridor_id,
    )


def _cells_continuous(end_cell: Cell, start_cell: Cell) -> bool:
    """Chain-continuity test from scope-v0:
        C_i's last cell is adjacent to C_{i+1}'s first cell,
        OR they share an anchor block (the CP block itself).
    Interpret ``adjacent`` as Chebyshev distance <= 1 (same cell
    counts as adjacent — covers the shared-anchor case)."""
    dx = abs(end_cell[0] - start_cell[0])
    dy = abs(end_cell[1] - start_cell[1])
    dz = abs(end_cell[2] - start_cell[2])
    return max(dx, dy, dz) <= 1


# ---------------------------------------------------------------------
# Pure assembly pass
# ---------------------------------------------------------------------

def assemble_route_from_inputs(
    inputs: AssemblyInputs,
) -> AssembledRoute | AssemblyError:
    """Pure function: produce an :class:`AssembledRoute` from already-
    fetched inputs, or an :class:`AssemblyError` if any gate fails.

    Applies the algorithm from scope-v0 step by step:

      1. Require Linked-CP; plain-CP short-circuits.
      2. Per interval, filter candidates to NOT NULL learned score,
         pick top by the pinned tie-break.
      3. Assert chain continuity between consecutive chosen corridors.
      4. Sum expected times; mean learned score for AI confidence.
    """
    # 0. Sanity on anchors themselves. An empty anchor sequence or a
    #    sequence without at least Spawn + Goal yields empty_corridors
    #    because we can't even form one interval.
    if len(inputs.anchors) < 2:
        return AssemblyError(
            reason="empty_corridors",
            detail=(
                f"anchor sequence has {len(inputs.anchors)} entries; "
                "need at least Spawn + Goal"
            ),
        )

    # 1. Linked-CP guard. Plain-CP short-circuits per scope-v0.
    if not inputs.is_linked_cp:
        return AssemblyError(
            reason="plain_cp_not_supported_v0",
            detail=(
                "v0 generation supports Linked-CP maps only; plain-CP "
                "interval ordering is ambiguous until per-CP alignment "
                "or OpenPlanet telemetry arrives"
            ),
        )

    # No corridor candidates at all → empty_corridors.
    if not inputs.candidates:
        return AssemblyError(
            reason="empty_corridors",
            detail="map has no route_corridors rows",
        )

    # Index candidates by (src_tag, src_order, dst_tag, dst_order) so
    # per-interval filtering is a cheap dict lookup.
    candidates_by_interval: dict[
        tuple[str, int, str, int], list[CandidateCorridor]
    ] = {}
    for c in inputs.candidates:
        key = (c.src.tag, c.src.order, c.dst.tag, c.dst.order)
        candidates_by_interval.setdefault(key, []).append(c)

    # 2. Walk anchor pairs, pick the top candidate per interval.
    intervals: list[IntervalAssembly] = []
    chosen_corridors: list[ChosenCorridor] = []
    for idx in range(len(inputs.anchors) - 1):
        src = inputs.anchors[idx]
        dst = inputs.anchors[idx + 1]
        pool = candidates_by_interval.get(
            (src.tag, src.order, dst.tag, dst.order), [],
        )
        scored_pool = [c for c in pool if c.learned_corridor_score is not None]
        if not scored_pool:
            return AssemblyError(
                reason="missing_corridor_in_interval",
                detail=(
                    f"no learned-scored corridor for interval "
                    f"{src.tag}#{src.order} → {dst.tag}#{dst.order}"
                ),
                interval_index=idx,
            )
        scored_pool.sort(key=_tie_break_key)
        top = scored_pool[0]
        assert top.learned_corridor_score is not None  # narrowed by filter
        chosen = ChosenCorridor(
            corridor_id=top.corridor_id,
            map_id=top.map_id,
            src=top.src,
            dst=top.dst,
            path_cells=top.path_cells,
            path_length=top.path_length,
            contains_virtual_edge=top.contains_virtual_edge,
            corridor_confidence=top.corridor_confidence,
            learned_corridor_score=float(top.learned_corridor_score),
            expected_time_ms=_expected_time_ms(top.path_length),
        )
        chosen_corridors.append(chosen)
        intervals.append(IntervalAssembly(
            index=idx, src=src, dst=dst, chosen=chosen,
        ))

    # 3. Chain continuity. The scope doc allows "adjacent OR shared
    #    anchor block" — the `_cells_continuous` helper treats equality
    #    as a valid case (distance 0). An empty path_cells on either
    #    side is a schema error (invalid_schema) not a chain break.
    for idx in range(len(chosen_corridors) - 1):
        this_c = chosen_corridors[idx]
        next_c = chosen_corridors[idx + 1]
        if not this_c.path_cells or not next_c.path_cells:
            return AssemblyError(
                reason="invalid_schema",
                detail=(
                    f"interval {idx} or {idx + 1} has empty path_cells"
                ),
                interval_index=idx,
            )
        end_cell = this_c.path_cells[-1]
        start_cell = next_c.path_cells[0]
        if not _cells_continuous(end_cell, start_cell):
            return AssemblyError(
                reason="chain_broken",
                detail=(
                    f"interval {idx} ends at {end_cell} but interval "
                    f"{idx + 1} starts at {start_cell}; Chebyshev "
                    "distance > 1 and no shared anchor block"
                ),
                interval_index=idx,
            )

    # 4. Aggregates.
    cells_total = sum(c.path_length for c in chosen_corridors)
    estimated_time_ms = sum(c.expected_time_ms for c in chosen_corridors)
    ai_confidence = (
        sum(c.learned_corridor_score for c in chosen_corridors)
        / len(chosen_corridors)
    )

    return AssembledRoute(
        map_id=inputs.map_id,
        anchors=inputs.anchors,
        intervals=tuple(intervals),
        cells_total=cells_total,
        estimated_time_ms=estimated_time_ms,
        ai_confidence=float(ai_confidence),
    )


# ---------------------------------------------------------------------
# DB wrapper
# ---------------------------------------------------------------------

_ANCHOR_QUERY = """
SELECT waypoint_index, waypoint_order, tag, x, y, z
FROM map_checkpoints
WHERE map_id = %s
  AND tag IN ('Spawn', 'Checkpoint', 'Goal')
ORDER BY
    CASE tag
        WHEN 'Spawn' THEN 0
        WHEN 'Checkpoint' THEN 1
        WHEN 'Goal' THEN 2
    END,
    waypoint_order
"""

_CORRIDORS_QUERY = """
SELECT id, map_id, src_tag, src_order, dst_tag, dst_order,
       path_cells, path_length, contains_virtual_edge,
       corridor_confidence, learned_corridor_score
FROM route_corridors
WHERE map_id = %s
  AND classification_version = %s
"""


def _parse_cells(raw: str) -> tuple[Cell, ...]:
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return ()
    out: list[Cell] = []
    for c in data:
        if isinstance(c, (list, tuple)) and len(c) == 3:
            try:
                out.append((int(c[0]), int(c[1]), int(c[2])))
            except (TypeError, ValueError):
                continue
    return tuple(out)


def _detect_and_order_anchors(
    rows: Sequence[tuple],
) -> tuple[bool, tuple[Anchor, ...]]:
    """Turn the map_checkpoints rows into (is_linked_cp, ordered anchors).

    Linked-CP detection: at least one Checkpoint row has waypoint_order
    >= 1. Otherwise plain-CP.

    Anchor ordering: Spawn first, then Checkpoints in ascending
    waypoint_order, then Goal. Ties on waypoint_order are resolved
    by waypoint_index (DB-returned secondary sort)."""
    linked = False
    spawn: Anchor | None = None
    goal: Anchor | None = None
    cps: list[Anchor] = []
    for r in rows:
        _idx, order, tag, x, y, z = r
        cell = (int(x), int(y), int(z)) if (
            x is not None and y is not None and z is not None
        ) else None
        anchor = Anchor(tag=str(tag), order=int(order), cell=cell)
        if tag == "Spawn":
            spawn = anchor
        elif tag == "Goal":
            goal = anchor
        elif tag == "Checkpoint":
            if int(order) >= 1:
                linked = True
            cps.append(anchor)
        # Other tags are ignored — route ends at Goal.
    ordered: list[Anchor] = []
    if spawn is not None:
        ordered.append(spawn)
    # Sort CPs by order then by tag insertion order (already idx-ascending).
    ordered.extend(sorted(cps, key=lambda a: a.order))
    if goal is not None:
        ordered.append(goal)
    return linked, tuple(ordered)


def assemble_route(
    conn: Connection,
    map_id: int,
    *,
    classification_version: str = CLASSIFICATION_VERSION,
) -> AssembledRoute | AssemblyError:
    """DB-facing wrapper. Fetches anchors + candidate corridors, then
    delegates to :func:`assemble_route_from_inputs`."""
    with cursor(conn) as cur:
        cur.execute(_ANCHOR_QUERY, (map_id,))
        anchor_rows = cur.fetchall()
        cur.execute(_CORRIDORS_QUERY, (map_id, classification_version))
        corridor_rows = cur.fetchall()

    linked, anchors = _detect_and_order_anchors(anchor_rows)
    candidates: list[CandidateCorridor] = []
    for r in corridor_rows:
        (
            cid, mid, src_tag, src_order, dst_tag, dst_order,
            path_cells_raw, path_length, virtual,
            conf, learned,
        ) = r
        cells = _parse_cells(path_cells_raw)
        candidates.append(CandidateCorridor(
            corridor_id=int(cid),
            map_id=int(mid),
            src=Anchor(tag=str(src_tag), order=int(src_order)),
            dst=Anchor(tag=str(dst_tag), order=int(dst_order)),
            path_cells=cells,
            path_length=int(path_length),
            contains_virtual_edge=bool(virtual),
            corridor_confidence=(
                float(conf) if conf is not None else None
            ),
            learned_corridor_score=(
                float(learned) if learned is not None else None
            ),
        ))

    return assemble_route_from_inputs(AssemblyInputs(
        map_id=map_id,
        is_linked_cp=linked,
        anchors=anchors,
        candidates=tuple(candidates),
    ))
