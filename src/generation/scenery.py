"""Scenery: dress the track with free-placed items.

Trees and flowers are NOT blocks in TM2020 — the earlier hunt for
``DecoTree*`` blocks only found them in the legacy TMNF collections.
Stadium2020 vegetation ships as **items** (``SpringTreeMedium``,
``CypressTall``, ``CactusMedium``, ...), listed in the game's own
``ItemInventory``: 718 Nadeo items, 40 of them Vegetation.

Using Nadeo items rather than the community assets embedded in corpus
maps matters for two reasons: they need no embedded PackDesc, and
they are not other people's work (the charter's attribution rule).

Items are free-placed, so unlike blocks they take absolute metres and
a yaw, not grid cells and a rotation index. One grid cell is 32 m
wide and 8 m tall in Stadium.

Placement obeys the charter's route-first rule: scenery is scattered
in cells the route does not occupy, never on it, and is generated
only after the route is final.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from src.catalogue.loader import BlockDef
from src.generation.clip_walker import GROUND_Y, Placement
from src.generation.supports import route_cells

_LOG = logging.getLogger(__name__)

SCENERY_VERSION = "scenery-v1"

# Stadium grid geometry, in metres per cell.
CELL_X = 32.0
CELL_Y = 8.0
CELL_Z = 32.0

# Absolute Y for an item standing on flat ground, in metres. Measured
# from a hand-placed reference map: trees on the ground sit at 8 m,
# even though the ground BLOCKS are grid row 9. Item Y and block Y are
# not the same scale, so ground_y * CELL_Y (= 72) put every tree 64 m
# in the air.
GROUND_ITEM_Y = 8.0

# Nadeo Stadium vegetation items, verified present in the game's
# ItemInventory. Grouped so a spec can ask for a mood rather than
# naming models.
PALETTES: dict[str, tuple[str, ...]] = {
    "spring": (
        "SpringTreeTall", "SpringTreeBig", "SpringTreeMedium",
        "SpringTreeSmall", "SpringTreeVerySmall",
        "SpringCherryTree", "CherryTreeMedium",
    ),
    "summer": ("SummerPalmTree", "PalmTreeMedium", "PalmTreeSmall"),
    "conifer": ("FirTall", "FirMedium", "CypressTall"),
    "desert": ("CactusMedium", "CactusVerySmall"),
}
DEFAULT_PALETTE = "spring"


@dataclass(frozen=True)
class Item:
    name: str
    x: float
    y: float
    z: float
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0


def _free_cells(
    occupied: set[tuple[int, int]],
    grid_min: int,
    grid_max: int,
    near: int,
) -> list[tuple[int, int]]:
    """Cells within ``near`` of the route but not part of it.

    Scenery hugging the track reads as landscaping; scenery scattered
    over the whole map reads as noise, so candidates are limited to a
    band around the route.
    """
    band: set[tuple[int, int]] = set()
    for x, z in occupied:
        for dx in range(-near, near + 1):
            for dz in range(-near, near + 1):
                cell = (x + dx, z + dz)
                if cell in occupied:
                    continue
                if not (grid_min <= cell[0] < grid_max):
                    continue
                if not (grid_min <= cell[1] < grid_max):
                    continue
                band.add(cell)
    return sorted(band)


def build_scenery(
    placements: list[Placement],
    catalogue: dict[str, BlockDef],
    seed: int = 1,
    palette: str = DEFAULT_PALETTE,
    density: float = 0.25,
    near: int = 3,
    grid_min: int = 0,
    grid_max: int = 48,
    ground_y: int = GROUND_Y,
) -> list[Item]:
    """Scatter vegetation around the route.

    ``density`` is the fraction of eligible cells that get an item, so
    0.0 is a bare map and 1.0 is a forest. Cells occupied by the route
    (at any height) are excluded, so nothing lands on the driving line
    or inside a pillar column.
    """
    if palette not in PALETTES:
        raise KeyError(
            f"unknown palette {palette!r}; available: {sorted(PALETTES)}"
        )
    models = PALETTES[palette]
    rng = random.Random(seed)

    cells3d = route_cells(placements, catalogue)
    # Project to XZ: a cell is off-limits at any height, so scenery
    # never appears under an elevated deck where it would clip.
    occupied = {(x, z) for x, _y, z in cells3d}

    candidates = _free_cells(occupied, grid_min, grid_max, near)
    take = int(len(candidates) * max(0.0, min(1.0, density)))
    chosen = rng.sample(candidates, take) if take else []

    items: list[Item] = []
    for cx, cz in chosen:
        # Jitter within the cell and rotate freely, so a scatter does
        # not read as a grid of identical trees.
        x = (cx + rng.uniform(0.2, 0.8)) * CELL_X
        z = (cz + rng.uniform(0.2, 0.8)) * CELL_Z
        y = GROUND_ITEM_Y + (ground_y - GROUND_Y) * CELL_Y
        items.append(Item(
            name=rng.choice(models),
            x=round(x, 3), y=round(y, 3), z=round(z, 3),
            yaw=round(rng.uniform(0.0, 6.283), 3),
        ))

    _LOG.info(
        "%s: %d items (palette=%s, density=%.2f) over %d candidate cells",
        SCENERY_VERSION, len(items), palette, density, len(candidates),
    )
    return items
