"""Support pillars under elevated route blocks.

Generated routes place only the driving line, so elevated sections
hang in mid-air — the single most obvious "this was not made by a
person" tell. Real maps do not look like that: in the corpus,
pillars are the most-placed blocks in TM2020 by a wide margin
(``DecoWallBasePillar`` alone has 22.6M placements, more than double
any road block), and inspection of real maps shows the rule is
mechanical — every column under an elevated block is filled from
ground level up to the block.

v1 fills each occupied XZ column with 1x1 ``DecoWallBasePillar``.
Real maps use footprint-matched pillars for curves
(``DecoWallCurve2Pillar`` under a 2x2, ``DecoWallCurve3Pillar``
under a 3x3), which looks tidier but needs the same
footprint/rotation reasoning as the walker; per-cell 1x1 pillars can
never overlap and are correct under any footprint, so shape matching
is deliberately left as a later refinement.

Supports are decoration: they are appended after the route is fixed
and never displace a route block (route-first rule, CLAUDE.md).
"""
from __future__ import annotations

import logging

from src.catalogue.loader import BlockDef, rotate_offset
from src.generation.clip_walker import GROUND_Y, Placement

_LOG = logging.getLogger(__name__)

SUPPORTS_VERSION = "supports-v1"

# 1x1x1, is_pillar, and the most-placed block in the TM2020 corpus.
DEFAULT_PILLAR = "DecoWallBasePillar"


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


def build_supports(
    placements: list[Placement],
    catalogue: dict[str, BlockDef],
    ground_y: int = GROUND_Y,
    pillar_id: str = DEFAULT_PILLAR,
) -> list[Placement]:
    """Pillar placements filling every column under the route.

    For each XZ column the route occupies, fill every empty cell from
    ``ground_y`` up to the HIGHEST route cell in that column. Filling
    only up to the lowest cell would leave an upper deck unsupported
    wherever a route dips back down and climbs again in the same
    column; route cells themselves are always skipped.
    """
    if pillar_id not in catalogue:
        raise KeyError(f"pillar block not in catalogue: {pillar_id}")

    cells = route_cells(placements, catalogue)
    highest: dict[tuple[int, int], int] = {}
    for x, y, z in cells:
        key = (x, z)
        prev = highest.get(key)
        if prev is None or y > prev:
            highest[key] = y

    supports: list[Placement] = []
    for (x, z), top in sorted(highest.items()):
        for y in range(ground_y, top):
            if (x, y, z) in cells:
                continue
            supports.append(Placement(pillar_id, x, y, z, 0))

    _LOG.info(
        "%s: %d pillars under %d columns (%d route cells)",
        SUPPORTS_VERSION, len(supports), len(highest), len(cells),
    )
    return supports
