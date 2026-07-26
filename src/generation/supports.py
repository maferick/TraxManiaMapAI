"""Support pillars under elevated route blocks.

Generated routes place only the driving line, so elevated sections
hang in mid-air — the single most obvious "this was not made by a
person" tell. Real maps do not look like that: in the corpus,
pillars are the most-placed blocks in TM2020 by a wide margin
(``DecoWallBasePillar`` alone has 22.6M placements, more than double
any road block), and inspection of real maps shows the rule is
mechanical — every column under an elevated block is filled from
ground level up to the block.

v2 places ONE footprint-matched pillar per level, at the block's own
anchor and rotation. v1's per-cell 1x1 fill was structurally valid
but visually wrong: under a wide curve, 1x1 pillars tile into a
solid concrete plateau instead of a support that follows the road
(confirmed in-game 2026-07-26 against a hand-placed reference).

The ``TrackWall*Pillar`` family mirrors the road catalogue almost
exactly — ``RoadTechCurve2`` -> ``TrackWallCurve2Pillar`` (both
2x1x2), chicanes included — so the mapping is a name rewrite with a
footprint assertion. Blocks with no matching pillar (gates, slopes)
are 1x1 in XZ and fall back to ``TrackWallStraightPillar``.

Supports are decoration: they are appended after the route is fixed
and never displace a route block (route-first rule, CLAUDE.md).
"""
from __future__ import annotations

import logging

from src.catalogue.loader import BlockDef, rotate_offset
from src.generation.clip_walker import GROUND_Y, Placement

_LOG = logging.getLogger(__name__)

SUPPORTS_VERSION = "supports-v2"

# 1x1x1 fallback for blocks with no shape-matched pillar (gates,
# slopes) — all of which occupy a single XZ cell.
DEFAULT_PILLAR = "TrackWallStraightPillar"


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
    """Footprint-matched pillars beneath every elevated route block.

    One pillar per level, placed at the road block's own anchor and
    rotation so the support follows the road's shape. A level is
    skipped when its footprint would intersect anything already
    placed (the route, or a pillar from an earlier block), which is
    what keeps self-crossing routes safe.
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
        for y in range(ground_y, base_y):
            want = _footprint(pillar, p.x, y, p.z, p.rotation, catalogue)
            if not want or any(c in occupied for c in want):
                continue
            supports.append(Placement(pillar, p.x, y, p.z, p.rotation))
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
