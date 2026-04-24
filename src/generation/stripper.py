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
STRIP_POLICY_NONE: str = "none"

# Axis-only 6-neighbourhood (no diagonals). Matches scope-v0.1
# §Level-2 "1-cell grid-axis halo only."
_AXIS_NEIGHBORS: tuple[tuple[int, int, int], ...] = (
    (+1,  0,  0), (-1,  0,  0),
    ( 0, +1,  0), ( 0, -1,  0),
    ( 0,  0, +1), ( 0,  0, -1),
)


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
) -> frozenset[Cell]:
    """Union of cells the strip policy keeps: every chosen-corridor
    path cell + halo, plus every anchor cell (multi-cell CPs contribute
    cells the assembler didn't route through but the game still
    registers as the same waypoint)."""
    kept: set[Cell] = set()
    for iv in route.intervals:
        for cell in iv.chosen.path_cells:
            kept.add(cell)
            if policy == STRIP_POLICY_HALO_AXIS_1:
                x, y, z = cell
                for dx, dy, dz in _AXIS_NEIGHBORS:
                    kept.add((x + dx, y + dy, z + dz))
    # Anchor cells always preserved, halo or not.
    for anchor in route.anchors:
        if anchor.cell is not None:
            kept.add(anchor.cell)
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
    policy: str = STRIP_POLICY_HALO_AXIS_1,
) -> StripResult:
    """Apply ``policy`` to ``base_blocks`` given the chosen ``route``.
    Returns a :class:`StripResult` with the filtered block list +
    continuity verdict. Raises ``ValueError`` on unknown policy."""
    if policy not in (STRIP_POLICY_HALO_AXIS_1, STRIP_POLICY_NONE):
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

    kept_cells = compute_kept_cells(route, policy=policy)
    stripped = filter_blocks_by_cells(base_blocks, kept_cells)
    intact, detail = verify_route_on_kept_cells(route, kept_cells)
    _LOG.info(
        "strip_route: policy=%s base_blocks=%d kept_blocks=%d kept_cells=%d intact=%s",
        policy, len(base_blocks), len(stripped), len(kept_cells), intact,
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
