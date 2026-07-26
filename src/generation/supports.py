"""Support pillars under elevated route blocks.

Generated routes place only the driving line, so elevated sections
hang in mid-air — the single most obvious "this was not made by a
person" tell. Real maps do not look like that: in the corpus,
pillars are the most-placed blocks in TM2020 by a wide margin
(``DecoWallBasePillar`` alone has 22.6M placements, more than double
any road block), and inspection of real maps shows the rule is
mechanical — every column under an elevated block is filled from
ground level up to the block.

v3 replicates what the game itself writes. The editor auto-generates
pillars when a human places an elevated block, but that only happens
on interactive placement — a map written straight to .Map.Gbx gets
none, and merely re-saving it in the editor does not backfill them
(both verified in-game 2026-07-26).

Ground truth, from diffing a map before/after one hand-placed
``RoadTechStraight`` at y=18 (the game added exactly nine blocks):

    TrackWallStraightPillar (x,17,z) dir=North variant=1   <- shaft
    ... variant=1 for every level down to y=11 ...
    TrackWallStraightPillar (x,10,z) dir=North variant=5   <- transition
    TrackWallStraightPillar (x, 9,z) dir=North variant=0   <- foot

So: pillars are always ``North`` regardless of the road's rotation,
and the stack is foot / transition / shaft by height, selected via
the block variant. Earlier attempts got this wrong in two ways —
v1 tiled 1x1 pillars per cell into a solid plateau, and v2 inherited
the road's rotation and left variants unset.

Shape matching still applies for multi-cell road blocks
(``RoadTechCurve2`` -> ``TrackWallCurve2Pillar``); the variant
pattern above is only confirmed for the 1x1 straight case, so wider
pillars are emitted with the shaft variant and no foot until we have
the same evidence for them.

Supports are decoration: they are appended after the route is fixed
and never displace a route block (route-first rule, CLAUDE.md).
"""
from __future__ import annotations

import logging

from src.catalogue.loader import BlockDef, rotate_offset
from src.generation.clip_walker import GROUND_Y, Placement

_LOG = logging.getLogger(__name__)

SUPPORTS_VERSION = "supports-v3"

# 1x1x1 fallback for blocks with no shape-matched pillar (gates,
# slopes) — all of which occupy a single XZ cell.
DEFAULT_PILLAR = "TrackWallStraightPillar"

# Pillars are written facing North whatever the road above does.
PILLAR_DIRECTION = 0

# Variant by height within the stack (observed on the 1x1 straight).
PILLAR_VARIANT_FOOT = 0        # at ground level
PILLAR_VARIANT_TRANSITION = 5  # exactly one level above ground
PILLAR_VARIANT_SHAFT = 1       # everything higher


def pillar_for(
    block_id: str,
    catalogue: dict[str, BlockDef],
) -> str | None:
    """Footprint-matched pillar for a road block, or None.

    ``RoadTechCurve2`` -> ``TrackWallCurve2Pillar``. The candidate is
    accepted only if its ground footprint matches the road block's in
    XZ, so a name coincidence can never place an overlapping pillar.
    """
    block = catalogue.get(block_id)
    if block is None:
        return None
    variant = block.variant("ground", 0)
    if variant is None:
        return None
    want = (variant.size[0], variant.size[2])

    candidates = []
    for prefix in ("RoadTech", "PlatformTech", "RoadDirt", "RoadBump", "RoadIce"):
        if block_id.startswith(prefix):
            stem = block_id[len(prefix):]
            candidates.append(f"TrackWall{stem}Pillar")
            candidates.append(f"DecoWall{stem}Pillar")
    for cand in candidates:
        other = catalogue.get(cand)
        if other is None:
            continue
        ov = other.variant("ground", 0)
        if ov is None:
            continue
        if (ov.size[0], ov.size[2]) == want:
            return cand

    # No shape match: only safe for single-cell footprints.
    if want == (1, 1) and DEFAULT_PILLAR in catalogue:
        return DEFAULT_PILLAR
    return None


def route_cells(
    placements: list[Placement],
    catalogue: dict[str, BlockDef],
) -> set[tuple[int, int, int]]:
    """Every world cell occupied by the given placements."""
    cells: set[tuple[int, int, int]] = set()
    for p in placements:
        block = catalogue.get(p.block_id)
        if block is None:
            continue
        variant = block.variant("ground", 0)
        if variant is None:
            continue
        for unit in variant.units:
            off = rotate_offset(unit.offset, p.rotation, variant.size)
            cells.add((p.x + off[0], p.y + off[1], p.z + off[2]))
    return cells


def _footprint(
    block_id: str,
    x: int,
    y: int,
    z: int,
    rotation: int,
    catalogue: dict[str, BlockDef],
) -> list[tuple[int, int, int]]:
    block = catalogue.get(block_id)
    if block is None:
        return []
    variant = block.variant("ground", 0)
    if variant is None:
        return []
    out = []
    for unit in variant.units:
        off = rotate_offset(unit.offset, rotation, variant.size)
        out.append((x + off[0], y + off[1], z + off[2]))
    return out


def build_supports(
    placements: list[Placement],
    catalogue: dict[str, BlockDef],
    ground_y: int = GROUND_Y,
) -> list[Placement]:
    """Pillars beneath every elevated route block, as the game writes them.

    One pillar per level. Single-cell columns reproduce the observed
    foot / transition / shaft variant stack facing North; multi-cell
    road blocks get their footprint-matched pillar at the road's own
    rotation (so the support follows the curve). A level is skipped
    when its footprint would intersect anything already placed — the
    route, or a pillar from an earlier block — which keeps
    self-crossing routes safe.
    """
    cells = route_cells(placements, catalogue)
    occupied = set(cells)
    supports: list[Placement] = []
    unmatched: set[str] = set()

    for p in placements:
        own = _footprint(p.block_id, p.x, p.y, p.z, p.rotation, catalogue)
        if not own:
            continue
        base_y = min(c[1] for c in own)
        if base_y <= ground_y:
            continue
        pillar = pillar_for(p.block_id, catalogue)
        if pillar is None:
            unmatched.add(p.block_id)
            continue

        single_cell = pillar == DEFAULT_PILLAR
        # A 1x1 pillar is rotation-independent, so use the game's
        # North; wider pillars must follow the road to stay aligned.
        rotation = PILLAR_DIRECTION if single_cell else p.rotation

        for y in range(ground_y, base_y):
            want = _footprint(pillar, p.x, y, p.z, rotation, catalogue)
            if not want or any(c in occupied for c in want):
                continue
            # The variant stack is a function of HEIGHT, not
            # footprint, so it applies to wide pillars too. Leaving
            # them unset defaulted every one to variant 0 — i.e. a
            # column of "foot" pieces, which is what produced the
            # mismatched panelling seen in-game on curve supports.
            if y == ground_y:
                variant = PILLAR_VARIANT_FOOT
            elif y == ground_y + 1:
                variant = PILLAR_VARIANT_TRANSITION
            else:
                variant = PILLAR_VARIANT_SHAFT
            supports.append(
                Placement(pillar, p.x, y, p.z, rotation, variant=variant)
            )
            occupied.update(want)

    if unmatched:
        _LOG.warning(
            "%s: no shape-matched pillar for %s — those columns are "
            "left unsupported rather than filled with a wrong shape",
            SUPPORTS_VERSION, sorted(unmatched),
        )
    _LOG.info(
        "%s: %d pillars for %d route blocks (%d route cells)",
        SUPPORTS_VERSION, len(supports), len(placements), len(cells),
    )
    return supports
