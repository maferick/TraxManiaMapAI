"""Resolve a captured trajectory to an ordered sequence of driven blocks.

This is the step everything since the telemetry rig has been building
toward. Geometry alone provably cannot identify the driven successor:
41.2% of route blocks have four face-adjacent route neighbours, so a
walk of the block graph is ambiguous at nearly every step. Telemetry
resolves the ambiguity: the car was somewhere, and the question is only
WHICH of the few blocks under it it was actually riding.

Formulation
-----------
Hidden state per step: one candidate placement (a `block_placements`
row), or one of two explicit non-block states:

* ``AIRBORNE`` -- kinematically ballistic. The car is on nothing, and
  forcing it onto a block would corrupt the sequence exactly at jumps,
  which are the interesting parts.
* ``OFF_SURFACE`` -- grounded but no candidate (terrain driving, zone
  interiors, missing geometry). Kept explicit rather than snapped to
  the nearest block: a wrong block in the sequence is worse than a
  labelled hole, because downstream training would learn the wrong
  transition as if it were observed.

Emission: candidate membership of the swept position. Transition prior,
in increasing cost: stay on the same placement; move to a placement
sharing a footprint cell face; enter/leave AIRBORNE or OFF_SURFACE;
teleport between non-adjacent placements (heavily penalised, never
impossible -- a respawn IS a teleport and the data contains them).

Swept segments, not points. The owner's design note is load-bearing: a
fast car covers several metres between samples, so matching sample
points alone skips short traversals entirely. Consecutive samples
further apart than SWEEP_STEP_M are subdivided linearly before
candidate lookup.

What this is NOT
----------------
Not a racing-line model and not a grammar consumer. The transition
prior is geometry-only (footprint adjacency) precisely so the output
can later score grammar priors without circularity: memory records that
grammar agreement stops being independent ground truth the moment the
grammar becomes the transition prior. The authoritative validation here
is checkpoint consistency -- at each checkpoint crossing the active
state must be waypoint-bearing -- plus Spawn/Goal at the ends.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from src.route.block_matcher import (
    CandidateIndex,
    Placement,
    classify_airborne,
)

_LOG = logging.getLogger(__name__)

DRIVEN_PATH_VERSION = "driven_path-0.1"

# States that are not placements.
AIRBORNE = -1
OFF_SURFACE = -2

# Subdivide sample pairs longer than this before candidate lookup.
# Half a cell edge: a segment shorter than 16m cannot cross a full
# 32m cell unseen.
SWEEP_STEP_M = 16.0

# Vertical rows to search for a candidate under the sampled position,
# matching the coverage tool's measured offset histogram (peak at 0,
# tail at -1).
ROW_TOLERANCE = (0, -1)

# Transition costs. Relative order is what matters: stay < adjacent <
# surface-change < teleport. Values are log-domain-ish weights tuned on
# the pilot captures; nothing downstream depends on their absolute
# scale.
COST_STAY = 0.0
COST_ADJACENT = 1.0
COST_STATE_CHANGE = 3.0
COST_TELEPORT = 40.0
# Being AIRBORNE/OFF_SURFACE while candidates exist costs a little, so
# the path prefers concrete blocks when the kinematics allow it.
COST_IGNORE_CANDIDATE = 2.0


@dataclass(frozen=True)
class Visit:
    """One stretch of consecutive samples on one state."""

    state: int                  # placement index, AIRBORNE or OFF_SURFACE
    block_type: str | None
    first_sample: int
    last_sample: int
    enter_ms: int
    exit_ms: int

    @property
    def duration_ms(self) -> int:
        return self.exit_ms - self.enter_ms


@dataclass
class DrivenPath:
    visits: list[Visit]
    # Diagnostics, per sample.
    states: list[int]
    checkpoint_hits: int = 0
    checkpoint_total: int = 0
    spawn_ok: bool | None = None
    goal_ok: bool | None = None
    stats: dict = field(default_factory=dict)


def _swept_candidates(
    index: CandidateIndex,
    a: Mapping[str, float],
    b: Mapping[str, float] | None,
) -> tuple[int, ...]:
    """Candidates along the segment a->b (or at a alone)."""
    points = [(a["x"], a["y"], a["z"])]
    if b is not None:
        dist = math.dist(
            (a["x"], a["y"], a["z"]), (b["x"], b["y"], b["z"])
        )
        if dist > SWEEP_STEP_M:
            n = int(dist // SWEEP_STEP_M)
            for k in range(1, n + 1):
                t = k / (n + 1)
                points.append((
                    a["x"] + (b["x"] - a["x"]) * t,
                    a["y"] + (b["y"] - a["y"]) * t,
                    a["z"] + (b["z"] - a["z"]) * t,
                ))
    seen: dict[int, None] = {}
    for (x, y, z) in points:
        for dy in ROW_TOLERANCE:
            for c in index.grid_candidates(x, y, z, dy=dy):
                seen.setdefault(c, None)
        for c in index.free_candidates(x, y, z):
            seen.setdefault(c, None)
    return tuple(seen)


def _adjacency(
    placements: Mapping[int, Placement],
    footprints: Mapping[int, frozenset],
) -> dict[int, frozenset]:
    """Placement -> placements sharing or facing a footprint cell.

    Face adjacency, not corner: memory records that corner contact
    produced slabs meeting at their corners with nothing to drive
    across, so a corner neighbour is not a continuation.
    """
    cell_owner: dict[tuple, list[int]] = {}
    for pid, cells in footprints.items():
        for c in cells:
            cell_owner.setdefault(c, []).append(pid)
    out: dict[int, set] = {pid: set() for pid in footprints}
    deltas = ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1),
              (0, 1, 0), (0, -1, 0), (0, 0, 0))
    for pid, cells in footprints.items():
        for (cx, cy, cz) in cells:
            for (dx, dy, dz) in deltas:
                for other in cell_owner.get((cx + dx, cy + dy, cz + dz), ()):
                    if other != pid:
                        out[pid].add(other)
    return {pid: frozenset(v) for pid, v in out.items()}


def extract_driven_path(
    samples: Sequence[Mapping[str, float]],
    placements: Sequence[Placement],
    catalogue,
    *,
    checkpoint_indices: Sequence[int] = (),
    waypoint_cells: Mapping[tuple[int, int, int], str] | None = None,
    free_anchor: str = "center",
    free_pad_m: float = 8.0,
) -> DrivenPath:
    """Viterbi over the trajectory. Returns the best state sequence."""
    from src.route.block_matcher import grid_footprint_cells, to_cell

    index = CandidateIndex(
        placements, catalogue, free_anchor=free_anchor, free_pad_m=free_pad_m
    )
    by_id = {p.index: p for p in placements}
    footprints = {
        p.index: frozenset(grid_footprint_cells(p, catalogue))
        for p in placements if not p.is_free
    }
    adjacent = _adjacency(by_id, footprints)
    air = classify_airborne(samples)
    n = len(samples)

    # Per-step state sets.
    step_states: list[tuple[int, ...]] = []
    for i in range(n):
        cands = _swept_candidates(
            index, samples[i], samples[i + 1] if i + 1 < n else None
        )
        states = list(cands)
        # The non-block states are always reachable; the costs decide.
        states.append(AIRBORNE if air[i] else OFF_SURFACE)
        step_states.append(tuple(states))

    # Viterbi. State sets are tiny (usually 1-4), so this is linear in
    # samples with a small constant.
    INF = float("inf")
    prev_cost: dict[int, float] = {}
    back: list[dict[int, int]] = []
    for s in step_states[0]:
        prev_cost[s] = (
            COST_IGNORE_CANDIDATE
            if s in (AIRBORNE, OFF_SURFACE) and len(step_states[0]) > 1
            else 0.0
        )
    for i in range(1, n):
        cur_cost: dict[int, float] = {}
        cur_back: dict[int, int] = {}
        for s in step_states[i]:
            best, best_prev = INF, None
            for p, pc in prev_cost.items():
                if p == s:
                    t = COST_STAY
                elif p in (AIRBORNE, OFF_SURFACE) or s in (AIRBORNE, OFF_SURFACE):
                    t = COST_STATE_CHANGE
                elif s in adjacent.get(p, ()):
                    t = COST_ADJACENT
                else:
                    t = COST_TELEPORT
                c = pc + t
                if c < best:
                    best, best_prev = c, p
            emit = (
                COST_IGNORE_CANDIDATE
                if s in (AIRBORNE, OFF_SURFACE) and len(step_states[i]) > 1
                else 0.0
            )
            cur_cost[s] = best + emit
            cur_back[s] = best_prev
        prev_cost = cur_cost
        back.append(cur_back)

    # Backtrace.
    state = min(prev_cost, key=prev_cost.get)  # type: ignore[arg-type]
    states = [state]
    for i in range(n - 2, -1, -1):
        state = back[i][state]
        states.append(state)
    states.reverse()

    # Collapse into visits.
    visits: list[Visit] = []
    start = 0
    for i in range(1, n + 1):
        if i < n and states[i] == states[start]:
            continue
        s = states[start]
        visits.append(Visit(
            state=s,
            block_type=by_id[s].block_type if s >= 0 else (
                "AIRBORNE" if s == AIRBORNE else "OFF_SURFACE"),
            first_sample=start,
            last_sample=i - 1,
            enter_ms=int(samples[start]["time_ms"]),
            exit_ms=int(samples[i - 1]["time_ms"]),
        ))
        start = i

    path = DrivenPath(visits=visits, states=states)

    # Checkpoint validation: within a small window of each crossing the
    # path must touch a waypoint-bearing cell. The window absorbs the
    # split-to-sample rounding (one 50ms sample either side).
    if checkpoint_indices and waypoint_cells:
        for ci in checkpoint_indices:
            path.checkpoint_total += 1
            hit = False
            for j in range(max(0, ci - 2), min(n, ci + 3)):
                s = samples[j]
                cell = to_cell(s["x"], s["y"], s["z"])
                for dy in (0, -1, -2):
                    if (cell[0], cell[1] + dy, cell[2]) in waypoint_cells:
                        hit = True
                        break
                if hit:
                    break
            path.checkpoint_hits += hit

    block_visits = [v for v in visits if v.state >= 0]
    path.stats = {
        "samples": n,
        "visits": len(visits),
        "block_visits": len(block_visits),
        "distinct_blocks": len({v.state for v in block_visits}),
        "airborne_visits": sum(1 for v in visits if v.state == AIRBORNE),
        "off_surface_visits": sum(
            1 for v in visits if v.state == OFF_SURFACE),
        "off_surface_ms": sum(
            v.duration_ms for v in visits if v.state == OFF_SURFACE),
        "teleports": _count_teleports(states, adjacent),
    }
    return path


def _count_teleports(states: Sequence[int], adjacent) -> int:
    n = 0
    for a, b in zip(states, states[1:]):
        if a >= 0 and b >= 0 and a != b and b not in adjacent.get(a, ()):
            n += 1
    return n
