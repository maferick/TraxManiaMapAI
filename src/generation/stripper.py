"""Level-2 strip-to-route — first real geometric mutation.

Takes a happy-path :class:`AssembledRoute` + the base map's block list
and produces a stripped block list that keeps only the cells along the
chosen route plus a small halo for ramp / support / entry-approach
blocks. The resulting ``map.blocks`` is a geometrically-different
shape from the base — a ribbon that forces the driver onto the
generator's chosen corridor chain rather than whatever alternate path
the full map would admit.

Design contract (scope-v0.1 §Level-2):

- **Strip policy** is explicit. Today the only supported policy is
  ``halo_axis_1`` (each path cell + its 6 grid-axis neighbours). No
  diagonals; deliberately conservative to avoid re-admitting paths
  the assembler didn't choose.
- **Anchor cells are always kept**, even if not on any chosen corridor
  (multi-cell CPs have cells the assembler didn't route through but
  the game still registers as the same waypoint). Dropping them
  would break in-game race structure.
- **Reject path is preserved**. If the stripped cells can't reproduce
  the chosen route's cell-continuity, the artifact is emitted anyway
  with ``reject_reason=stripped_route_broken`` — the diagnostic signal
  is the point. scope-v0.1 explicitly allows save-on-reject so the
  operator can open the (broken) GBX and see where the halo wasn't
  big enough.

Consumers:
- :func:`src.generation.generator.generate_from_base` calls into
  :func:`strip_route` when ``inputs.strip`` is True.
- :func:`src.generation.gbx_writer.emit_gbx_from_artifact` reads the
  stripped ``map.blocks`` + forwards their cells as ``keep_cells`` to
  the C# :mod:`MapEmitter`, which filters ``CGameCtnChallenge.Blocks``
  before saving.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from src.generation.types import AssembledRoute, Cell

_LOG = logging.getLogger(__name__)

STRIP_POLICY_HALO_AXIS_1: str = "halo_axis_1"
STRIP_POLICY_HALO_AXIS_1_PLUS_ANCHOR_RADIUS_3: str = (
    "halo_axis_1_plus_anchor_radius_3"
)
STRIP_POLICY_HALO_AXIS_1_PLUS_ANCHOR_RADIUS_3_VEXT_3: str = (
    "halo_axis_1_plus_anchor_radius_3_vext_3"
)
STRIP_POLICY_HALO_XZ_CHEB_1_VEXT_3_PLUS_ANCHOR_RADIUS_3: str = (
    "halo_xz_cheb_1_vext_3_plus_anchor_radius_3"
)
STRIP_POLICY_HALO_PRISM_3X7X3_PLUS_ANCHOR_RADIUS_3: str = (
    "halo_prism_3x7x3_plus_anchor_radius_3"
)
STRIP_POLICY_NONE: str = "none"

# Radius of the anchor-preservation cube used by
# ``halo_axis_1_plus_anchor_radius_3``. Chebyshev distance from each
# anchor cell: 3 → a 7×7×7 = 343-cell cube per anchor. Sized to cover
# TM2020's multi-block start-curve / finish-gate / checkpoint-ramp
# assemblies which span 3-5 cells radially (map 1212's
# PlatformPlasticLoopOutStartCurve1 cluster is the canonical example —
# see PR L diagnosis).
_ANCHOR_RADIUS_CHEB: int = 3

# Vertical extension around every route path cell for
# ``halo_axis_1_plus_anchor_radius_3_vext_3``. Covers support /
# pillar / base geometry sitting below or above the drivable surface:
# straightforward road tracks typically have pillar columns 1-3 cells
# below the drive cell, and the earlier policy missed those because
# the axis-1 halo only reached ±1 and the anchor cube only covers
# anchor-proximal cells. +/-3 in Y extends the per-path-cell halo
# specifically along the vertical axis without widening the whole
# Chebyshev cube (that'd add far too many cells).
_PATH_VERTICAL_EXT: int = 3

# Axis-only 6-neighbourhood (no diagonals). Matches scope-v0.1
# §Level-2 "1-cell grid-axis halo only."
_AXIS_NEIGHBORS: tuple[tuple[int, int, int], ...] = (
    (+1,  0,  0), (-1,  0,  0),
    ( 0, +1,  0), ( 0, -1,  0),
    ( 0,  0, +1), ( 0,  0, -1),
)

# 3×3 XZ neighbourhood at the same Y (the 8 cells surrounding the
# path cell in its horizontal plane). Used by the #217-b quick-patch
# policy to catch wall / transition / slope blocks sitting at
# XZ-diagonal offsets from the route — the canonical drop pattern
# surfaced by the PR #56 diagnostic on map 1212 cell (31, 13, 22).
_XZ_CHEB_1_NEIGHBORS: tuple[tuple[int, int, int], ...] = tuple(
    (dx, 0, dz)
    for dx in (-1, 0, 1)
    for dz in (-1, 0, 1)
    if (dx, dz) != (0, 0)
)


def _cheb_cube(centre: Cell, radius: int) -> set[Cell]:
    """Every grid cell within Chebyshev distance ``radius`` of
    ``centre``, inclusive. ``radius=3`` → 343 cells."""
    cx, cy, cz = centre
    out: set[Cell] = set()
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                out.add((cx + dx, cy + dy, cz + dz))
    return out


@dataclass(frozen=True)
class StripResult:
    """Return value of :func:`strip_route`. Keeps the metadata the
    artifact needs on `map.*` plus the continuity verdict the gate
    consumes to decide route_verified."""
    stripped_blocks: list[dict[str, Any]]
    kept_cells: frozenset[Cell]
    kept_block_count: int
    base_block_count: int
    strip_policy: str
    route_intact: bool
    broken_detail: str | None  # human-readable; None when route_intact


# ---------------------------------------------------------------------
# Cell-set construction
# ---------------------------------------------------------------------

def compute_kept_cells(
    route: AssembledRoute,
    *,
    policy: str = STRIP_POLICY_HALO_AXIS_1,
    anchor_cells: frozenset[Cell] | None = None,
) -> frozenset[Cell]:
    """Union of cells the strip policy keeps.

    Always included:
    - Every chosen-corridor path cell (so the route itself survives).
    - Every anchor cell carried on :class:`Anchor.cell`
      (multi-cell grid anchors contribute cells the assembler didn't
      route through but the game still registers as the same waypoint).

    Policy-specific additions:

    - ``halo_axis_1`` (scope-v0.1 default in PR #45): 1-cell grid-axis
      halo around every path cell. Conservative; loses multi-block
      start-ramp / finish-gate geometry when the assembly doesn't step
      through all the block's cells (map 1212 Spawn bug — see PR L).
    - ``halo_axis_1_plus_anchor_radius_3`` (new, default from PR L):
      everything above PLUS a 7×7×7 Chebyshev cube around every cell
      in ``anchor_cells`` (which includes grid anchors and
      snapped-to-grid free-placed anchors). Covers multi-block anchor
      assemblies; preserves spawn / CP / finish ramp geometry.

    ``anchor_cells`` is only consulted when the active policy uses it.
    For the other policies the arg may be None.
    """
    kept: set[Cell] = set()
    uses_anchor_radius = policy in (
        STRIP_POLICY_HALO_AXIS_1_PLUS_ANCHOR_RADIUS_3,
        STRIP_POLICY_HALO_AXIS_1_PLUS_ANCHOR_RADIUS_3_VEXT_3,
        STRIP_POLICY_HALO_XZ_CHEB_1_VEXT_3_PLUS_ANCHOR_RADIUS_3,
        STRIP_POLICY_HALO_PRISM_3X7X3_PLUS_ANCHOR_RADIUS_3,
    )
    uses_path_axis_halo = (
        policy == STRIP_POLICY_HALO_AXIS_1
        or policy == STRIP_POLICY_HALO_AXIS_1_PLUS_ANCHOR_RADIUS_3
        or policy == STRIP_POLICY_HALO_AXIS_1_PLUS_ANCHOR_RADIUS_3_VEXT_3
    )
    uses_path_xz_cheb1 = (
        policy == STRIP_POLICY_HALO_XZ_CHEB_1_VEXT_3_PLUS_ANCHOR_RADIUS_3
    )
    uses_vertical_ext = policy in (
        STRIP_POLICY_HALO_AXIS_1_PLUS_ANCHOR_RADIUS_3_VEXT_3,
        STRIP_POLICY_HALO_XZ_CHEB_1_VEXT_3_PLUS_ANCHOR_RADIUS_3,
    )
    uses_prism = (
        policy == STRIP_POLICY_HALO_PRISM_3X7X3_PLUS_ANCHOR_RADIUS_3
    )

    for iv in route.intervals:
        for cell in iv.chosen.path_cells:
            kept.add(cell)
            x, y, z = cell
            if uses_path_axis_halo:
                for dx, dy, dz in _AXIS_NEIGHBORS:
                    kept.add((x + dx, y + dy, z + dz))
            if uses_path_xz_cheb1:
                # Full 3×3 horizontal neighbourhood at the same Y.
                # Catches wall / transition / slope blocks sitting
                # at XZ-diagonal offsets that axis-1 skips.
                for dx, dy, dz in _XZ_CHEB_1_NEIGHBORS:
                    kept.add((x + dx, y + dy, z + dz))
                # Axis-1 Y neighbours are kept by vext, so no need
                # to add ±Y here separately.
            if uses_vertical_ext:
                # Vertical-only column around this path cell, ±N cells
                # on Y. Captures pillars / bases / structural supports
                # that sit directly below or above the drivable surface
                # but outside both the axis-1 halo and any anchor cube.
                for dy in range(-_PATH_VERTICAL_EXT, _PATH_VERTICAL_EXT + 1):
                    if dy == 0:
                        continue  # path cell itself already added
                    kept.add((x, y + dy, z))
            if uses_prism:
                # Full 3×7×3 prism: the 3×3 XZ neighbourhood at every
                # Y in the ±3 range around the path cell. Subsumes
                # xz_cheb_1 + vext_3 into one volume so that wall /
                # transition / slope blocks sitting at *XZ-diagonal
                # AND ±Y* from the route cell are captured too.
                # Previous policy's per-path-cell cell count ≈ 15;
                # this one is 63 → ~4× growth, with corresponding
                # increase in total kept cells. Use only when the
                # map's structural envelope genuinely needs it
                # (in-game testing of map 1212 after #217-b showed
                # y±1 XZ-diagonal drops still breaking drivability).
                for dx in (-1, 0, 1):
                    for dy in range(
                        -_PATH_VERTICAL_EXT, _PATH_VERTICAL_EXT + 1
                    ):
                        for dz in (-1, 0, 1):
                            kept.add((x + dx, y + dy, z + dz))

    # Anchor cells from Anchor.cell (grid anchors only — free anchors
    # arrive via the ``anchor_cells`` arg below).
    for anchor in route.anchors:
        if anchor.cell is not None:
            kept.add(anchor.cell)

    if uses_anchor_radius and anchor_cells:
        for anchor_cell in anchor_cells:
            kept.update(_cheb_cube(anchor_cell, _ANCHOR_RADIUS_CHEB))

    return frozenset(kept)


# ---------------------------------------------------------------------
# Block filtering
# ---------------------------------------------------------------------

def _block_cell(block: dict[str, Any]) -> Cell | None:
    """Return the ``(x, y, z)`` of a grid-placed block, or None if
    it's free-placed (NULL x/y/z) or schema-invalid."""
    x, y, z = block.get("x"), block.get("y"), block.get("z")
    if x is None or y is None or z is None:
        return None
    try:
        return (int(x), int(y), int(z))
    except (TypeError, ValueError):
        return None


def filter_blocks_by_cells(
    blocks: Iterable[dict[str, Any]], kept_cells: frozenset[Cell],
) -> list[dict[str, Any]]:
    """Keep only grid blocks whose cell is in ``kept_cells``. Free-
    placed blocks (NULL coords) are dropped defensively — the v0
    schema doesn't carry free blocks anyway, but a future schema rev
    might, and we don't want to leak them through the strip."""
    out: list[dict[str, Any]] = []
    for b in blocks:
        cell = _block_cell(b)
        if cell is None:
            continue
        if cell in kept_cells:
            out.append(b)
    return out


# ---------------------------------------------------------------------
# Continuity verification on the stripped set
# ---------------------------------------------------------------------

def verify_route_on_kept_cells(
    route: AssembledRoute, kept_cells: frozenset[Cell],
) -> tuple[bool, str | None]:
    """Check the chosen route's cell-continuity survives the strip.

    Under ``halo_axis_1`` every path cell is trivially kept (the path
    cells are what we built the halo around), so this check is
    tautologically True for the default policy. The infrastructure
    lives here so stricter future policies (halo=0, path-cells-only)
    fire ``stripped_route_broken`` honestly without new plumbing.
    """
    for iv_idx, iv in enumerate(route.intervals):
        for cell_idx, cell in enumerate(iv.chosen.path_cells):
            if cell not in kept_cells:
                return (
                    False,
                    f"interval {iv_idx} corridor {iv.chosen.corridor_id} "
                    f"cell #{cell_idx} {cell} dropped by strip — "
                    f"halo too tight for this route",
                )
    return True, None


# ---------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------

def strip_route(
    route: AssembledRoute,
    base_blocks: list[dict[str, Any]],
    *,
    policy: str = STRIP_POLICY_HALO_PRISM_3X7X3_PLUS_ANCHOR_RADIUS_3,
    anchor_cells: frozenset[Cell] | None = None,
) -> StripResult:
    """Apply ``policy`` to ``base_blocks`` given the chosen ``route``.
    Returns a :class:`StripResult` with the filtered block list +
    continuity verdict. Raises ``ValueError`` on unknown policy.

    ``anchor_cells`` is the union of every waypoint's grid cell —
    grid-placed anchors directly, free-placed anchors after snapping
    via TM2020's fixed block dimensions (caller's responsibility).
    The ``halo_axis_1_plus_anchor_radius_3`` policy uses it to grow
    a preservation cube around every anchor; other policies ignore it.
    """
    known_policies = (
        STRIP_POLICY_HALO_AXIS_1,
        STRIP_POLICY_HALO_AXIS_1_PLUS_ANCHOR_RADIUS_3,
        STRIP_POLICY_HALO_AXIS_1_PLUS_ANCHOR_RADIUS_3_VEXT_3,
        STRIP_POLICY_HALO_XZ_CHEB_1_VEXT_3_PLUS_ANCHOR_RADIUS_3,
        STRIP_POLICY_HALO_PRISM_3X7X3_PLUS_ANCHOR_RADIUS_3,
        STRIP_POLICY_NONE,
    )
    if policy not in known_policies:
        raise ValueError(f"unknown strip policy: {policy!r}")

    if policy == STRIP_POLICY_NONE:
        # No-op: return base blocks verbatim + metadata reflecting that.
        return StripResult(
            stripped_blocks=list(base_blocks),
            kept_cells=frozenset(),
            kept_block_count=len(base_blocks),
            base_block_count=len(base_blocks),
            strip_policy=STRIP_POLICY_NONE,
            route_intact=True,
            broken_detail=None,
        )

    kept_cells = compute_kept_cells(
        route, policy=policy, anchor_cells=anchor_cells,
    )
    stripped = filter_blocks_by_cells(base_blocks, kept_cells)
    intact, detail = verify_route_on_kept_cells(route, kept_cells)
    _LOG.info(
        "strip_route: policy=%s base_blocks=%d kept_blocks=%d "
        "kept_cells=%d anchor_cells=%d intact=%s",
        policy, len(base_blocks), len(stripped), len(kept_cells),
        len(anchor_cells or ()), intact,
    )
    return StripResult(
        stripped_blocks=stripped,
        kept_cells=kept_cells,
        kept_block_count=len(stripped),
        base_block_count=len(base_blocks),
        strip_policy=policy,
        route_intact=intact,
        broken_detail=detail,
    )
