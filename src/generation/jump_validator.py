"""Jump-aware geometry validator (task #227).

An approximate Layer-1 jump detector — NOT a physics simulator. TM2020's
public physics isn't fully specified, so instead of recreating the
engine we pair three cheap signals:

  1. a ramp / slope / loop block at the takeoff cell (shape_class
     from the #218-6 catalogue),
  2. a plausible landing surface inside a configurable forward cone
     (distance × height × lateral spread),
  3. optional replay evidence that a driver has already cleared this
     transition ("observed_traversable", per the CLAUDE.md replay-
     ground-truth learning contract).

The module classifies each candidate jump into one of four buckets —
``supported_by_replay`` / ``geometrically_plausible`` / ``uncertain``
/ ``likely_broken`` — and returns findings. It does NOT hard-reject
uncertain jumps, does NOT mutate traversability, and does NOT bypass
the finishability gate.

Primary use cases:

  - flag strip / generation failures where the landing block was
    removed (likely_broken),
  - soft signal for generation scoring (uncertain jumps cost the
    generator a bit but aren't vetoed),
  - pre-flight check before emit-gbx to surface obvious mistakes
    before the round-trip.

OpenPlanet telemetry — per-frame position data from a live game
session — remains the eventual ground-truth path. This validator's
geometric heuristics fill the gap until that lands.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from src.generation.geom_validator import (
    Cell,
    Finding,
    GeometryInfo,
    SEVERITY_FAIL,
    SEVERITY_INFO,
    SEVERITY_WARN,
    _chebyshev,
    _lookup_key,
)

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Classification — closed vocabulary.
# ---------------------------------------------------------------------

CLASS_SUPPORTED_BY_REPLAY = "supported_by_replay"
CLASS_GEOMETRICALLY_PLAUSIBLE = "geometrically_plausible"
CLASS_UNCERTAIN = "uncertain"
CLASS_LIKELY_BROKEN = "likely_broken"

_CLASS_TO_SEVERITY: dict[str, str] = {
    CLASS_SUPPORTED_BY_REPLAY: SEVERITY_INFO,
    CLASS_GEOMETRICALLY_PLAUSIBLE: SEVERITY_INFO,
    CLASS_UNCERTAIN: SEVERITY_WARN,
    CLASS_LIKELY_BROKEN: SEVERITY_FAIL,
}

CODE_JUMP = "jump"


# ---------------------------------------------------------------------
# Tunables — operator-configurable but every default is conservative.
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class JumpConeConfig:
    """Search-cone parameters for landing-surface detection.

    All distances are in grid cells (TM2020 = 32m per cell). The
    cone extends forward along the takeoff block's connector axis
    when one is known, otherwise it degenerates to an axis-aligned
    box around the takeoff cell.

    Defaults are deliberately conservative — jumps that barely fit
    under these bounds get classified ``geometrically_plausible``;
    anything wider than the cone gets ``uncertain`` unless a replay
    vouches for it.
    """
    forward_min_cells: int = 2
    forward_max_cells: int = 12
    max_rise_cells: int = 4
    max_drop_cells: int = 8
    lateral_half_width: int = 2

    def __post_init__(self) -> None:
        if self.forward_min_cells <= 0:
            raise ValueError("forward_min_cells must be positive")
        if self.forward_max_cells < self.forward_min_cells:
            raise ValueError(
                "forward_max_cells must be >= forward_min_cells",
            )


# Shapes whose presence at the takeoff cell is a jump cue.
_TAKEOFF_SHAPES: frozenset[str] = frozenset({"ramp", "loop"})

# Shapes whose presence at a candidate landing cell counts as a
# plausible surface. Loops land on themselves; ramps may land on
# straights or platforms; curves connect in XZ-plane so they land
# OK. Support / deco / anchor shapes don't count.
_LANDING_SHAPES: frozenset[str] = frozenset({
    "straight", "curve", "ramp", "platform", "loop",
    "start", "checkpoint", "finish",
})


# ---------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class JumpCandidate:
    takeoff_cell: Cell
    next_route_cell: Cell
    gap_cheb: int
    takeoff_shape: str
    takeoff_family: str
    takeoff_name: str


@dataclass(frozen=True)
class JumpClassification:
    candidate: JumpCandidate
    classification: str
    detail: str
    landing_candidates: tuple[Cell, ...] = ()


@dataclass
class JumpReport:
    classifications: list[JumpClassification] = field(default_factory=list)
    cone: JumpConeConfig = field(default_factory=JumpConeConfig)
    route_cells_total: int = 0

    def by_class(self, cls: str) -> list[JumpClassification]:
        return [c for c in self.classifications if c.classification == cls]

    def findings(self) -> list[Finding]:
        """Convert classifications into validator findings.

        ``likely_broken`` → FAIL, ``uncertain`` → WARN, the two
        ``*plausible`` classes → INFO. Consumers that just want the
        geom_validator-shaped view can treat this list as a drop-in
        addition to :class:`ValidationReport.findings`.
        """
        out: list[Finding] = []
        for c in self.classifications:
            severity = _CLASS_TO_SEVERITY.get(c.classification, SEVERITY_WARN)
            out.append(Finding(
                severity=severity,
                code=CODE_JUMP,
                detail=f"{c.classification}: {c.detail}",
                cell=c.candidate.takeoff_cell,
            ))
        return out


# ---------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------

def detect_jump_candidates(
    *,
    route_cells: list[Cell],
    cell_to_block: Mapping[Cell, Mapping],
    geometry_lookup: Mapping[tuple[str, str], GeometryInfo],
    min_gap_cheb: int = 2,
) -> list[JumpCandidate]:
    """Walk the route and return cells where a jump is likely in play.

    A candidate is any route-cell pair (A, B) where EITHER:

      - the Chebyshev distance A→B exceeds ``min_gap_cheb`` (explicit
        gap the ground path can't cross), OR
      - A carries a ``ramp`` / ``loop`` block (geometry cue that the
        car is about to leave the ground, regardless of the next
        cell's distance).

    The second case matters because a route can chain "ramp → ground
    cell directly adjacent" (cheb=1) while still involving a real
    jump that the strip policy could silently break.
    """
    candidates: list[JumpCandidate] = []
    if len(route_cells) < 2:
        return candidates

    for i in range(1, len(route_cells)):
        prev, nxt = route_cells[i - 1], route_cells[i]
        gap = _chebyshev(prev, nxt)
        block = cell_to_block.get(prev)
        shape = "unknown"
        family = ""
        name = ""
        if block is not None:
            family = str(block.get("family") or "")
            name = str(block.get("name") or "")
            info = geometry_lookup.get(_lookup_key(family, name))
            if info is not None:
                shape = info.shape_class

        is_takeoff_shape = shape in _TAKEOFF_SHAPES
        is_explicit_gap = gap >= min_gap_cheb
        if not (is_takeoff_shape or is_explicit_gap):
            continue

        candidates.append(JumpCandidate(
            takeoff_cell=prev,
            next_route_cell=nxt,
            gap_cheb=gap,
            takeoff_shape=shape,
            takeoff_family=family,
            takeoff_name=name,
        ))

    return candidates


def find_landing_candidates(
    *,
    candidate: JumpCandidate,
    cell_to_block: Mapping[Cell, Mapping],
    geometry_lookup: Mapping[tuple[str, str], GeometryInfo],
    cone: JumpConeConfig,
) -> list[Cell]:
    """Cells inside the forward cone that carry a landing-class shape.

    The forward axis is inferred from the vector to ``next_route_cell``
    (so the cone actually points where the route is trying to go).
    If that vector is degenerate — zero in both X and Z — we fall
    back to an XZ-axis-aligned search around the takeoff cell.
    """
    ox, oy, oz = candidate.takeoff_cell
    nx, _, nz = candidate.next_route_cell
    dx, dz = nx - ox, nz - oz

    # Pick the dominant horizontal axis; tie-break on X.
    if abs(dx) >= abs(dz) and dx != 0:
        axis = (1 if dx > 0 else -1, 0)
    elif dz != 0:
        axis = (0, 1 if dz > 0 else -1)
    else:
        # No clear forward — scan a box around takeoff.
        axis = (1, 0)

    out: list[Cell] = []
    fwd_x, fwd_z = axis
    for step in range(cone.forward_min_cells, cone.forward_max_cells + 1):
        cx = ox + fwd_x * step
        cz = oz + fwd_z * step
        # The "lateral" axis is the one orthogonal to forward in XZ.
        lat_x, lat_z = -fwd_z, fwd_x
        for lat in range(-cone.lateral_half_width, cone.lateral_half_width + 1):
            px = cx + lat_x * lat
            pz = cz + lat_z * lat
            for dy in range(-cone.max_drop_cells, cone.max_rise_cells + 1):
                cell = (px, oy + dy, pz)
                block = cell_to_block.get(cell)
                if block is None:
                    continue
                info = geometry_lookup.get(_lookup_key(
                    str(block.get("family") or ""),
                    str(block.get("name") or ""),
                ))
                if info is None:
                    continue
                if info.shape_class in _LANDING_SHAPES:
                    out.append(cell)
    return out


def classify_jump(
    *,
    candidate: JumpCandidate,
    landings: list[Cell],
    replay_touched_cells: set[Cell] | None,
) -> JumpClassification:
    """Bucket a candidate into one of the four classes.

    Precedence (first matching wins):

      1. ``supported_by_replay`` — a replay's driven cells cover BOTH
         the takeoff and at least one cell adjacent (cheb=1) to the
         next_route_cell or a landing candidate. Replay cells are
         authoritative per CLAUDE.md, so this class beats geometry.
      2. ``geometrically_plausible`` — at least one landing candidate,
         AND the next_route_cell itself is cheb<=1 from some landing.
      3. ``likely_broken`` — a takeoff shape is present and we found
         no landing candidates in the cone at all.
      4. ``uncertain`` — fallback. Typically: gap exists but takeoff
         shape doesn't scream "jump", or landings exist but none
         aligns with the route's next cell.
    """
    if replay_touched_cells is not None:
        if (
            candidate.takeoff_cell in replay_touched_cells
            and any(
                _chebyshev(candidate.next_route_cell, c) <= 1
                for c in replay_touched_cells
            )
        ):
            return JumpClassification(
                candidate=candidate,
                classification=CLASS_SUPPORTED_BY_REPLAY,
                detail=(
                    f"replay crosses {candidate.takeoff_cell} → "
                    f"{candidate.next_route_cell} (gap={candidate.gap_cheb})"
                ),
                landing_candidates=tuple(landings),
            )

    if not landings:
        if candidate.takeoff_shape in _TAKEOFF_SHAPES:
            return JumpClassification(
                candidate=candidate,
                classification=CLASS_LIKELY_BROKEN,
                detail=(
                    f"takeoff shape={candidate.takeoff_shape} with no "
                    f"landing surface in forward cone"
                ),
            )
        return JumpClassification(
            candidate=candidate,
            classification=CLASS_UNCERTAIN,
            detail=(
                f"gap={candidate.gap_cheb} with no ramp cue and no "
                f"landing in cone"
            ),
        )

    # Landings exist — is next_route_cell aligned with any of them?
    if any(_chebyshev(candidate.next_route_cell, c) <= 1 for c in landings):
        return JumpClassification(
            candidate=candidate,
            classification=CLASS_GEOMETRICALLY_PLAUSIBLE,
            detail=(
                f"{len(landings)} landing(s) in cone, route-aligned"
            ),
            landing_candidates=tuple(landings),
        )

    return JumpClassification(
        candidate=candidate,
        classification=CLASS_UNCERTAIN,
        detail=(
            f"{len(landings)} landing(s) in cone but none within cheb=1 "
            f"of next route cell {candidate.next_route_cell}"
        ),
        landing_candidates=tuple(landings),
    )


# ---------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------

def validate_jumps(
    *,
    blocks: Iterable[Mapping],
    geometry_lookup: Mapping[tuple[str, str], GeometryInfo],
    route_cells: list[Cell],
    replay_touched_cells: set[Cell] | None = None,
    cone: JumpConeConfig | None = None,
    min_gap_cheb: int = 2,
) -> JumpReport:
    """Detect + classify every jump candidate along ``route_cells``.

    ``replay_touched_cells`` is the union of cells any clean replay
    for this map's input has passed through (or None when no replay
    evidence is available). When None, classifications fall back
    purely on geometry — still useful, but never produces
    ``supported_by_replay``.
    """
    cone = cone or JumpConeConfig()
    cell_to_block: dict[Cell, Mapping] = {}
    for b in blocks:
        if b.get("placement") != "grid":
            continue
        try:
            c = (int(b["x"]), int(b["y"]), int(b["z"]))
        except (KeyError, TypeError, ValueError):
            continue
        cell_to_block.setdefault(c, b)

    report = JumpReport(cone=cone, route_cells_total=len(route_cells))
    candidates = detect_jump_candidates(
        route_cells=route_cells,
        cell_to_block=cell_to_block,
        geometry_lookup=geometry_lookup,
        min_gap_cheb=min_gap_cheb,
    )

    for cand in candidates:
        landings = find_landing_candidates(
            candidate=cand,
            cell_to_block=cell_to_block,
            geometry_lookup=geometry_lookup,
            cone=cone,
        )
        report.classifications.append(classify_jump(
            candidate=cand,
            landings=landings,
            replay_touched_cells=replay_touched_cells,
        ))

    _LOG.info(
        "validate_jumps: route_cells=%d candidates=%d "
        "supported=%d plausible=%d uncertain=%d broken=%d",
        report.route_cells_total, len(candidates),
        len(report.by_class(CLASS_SUPPORTED_BY_REPLAY)),
        len(report.by_class(CLASS_GEOMETRICALLY_PLAUSIBLE)),
        len(report.by_class(CLASS_UNCERTAIN)),
        len(report.by_class(CLASS_LIKELY_BROKEN)),
    )
    return report
