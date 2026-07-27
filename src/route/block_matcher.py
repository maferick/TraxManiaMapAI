"""Resolve telemetry positions to candidate blocks.

Route inference treats block identity as a hidden state with telemetry
as the emission (see the Viterbi plan). That needs, per sample, the set
of blocks the car could plausibly be on. This module generates those
candidates. It deliberately does NOT decide which candidate is correct;
over-generation here is cheap, and a missing candidate is fatal because
the true block can never be recovered downstream.

Calibration
-----------
Confirmed 2026-07-27 against 17 validated captures over 58k placements:
90.0% of driven samples land exactly on a recorded block anchor, 99.9%
within two cells, nothing beyond three. No systematic offset or scale
error.

    cell_x = floor(x / 32)
    cell_y = floor(9 + (y - 8) / 8)
    cell_z = floor(z / 32)

Two matching paths, because the corpus needs both
-------------------------------------------------
1. **Grid placements** carry integer cell coords and a rotation, but
   `block_placements` stores only the ANCHOR cell. Real blocks span
   multiple cells, so a car on the far end of a 1x2 lands in a cell with
   no row of its own. That is precisely the residual measured above:
   misses cluster at Chebyshev distance 1, not scattered. So every
   placement is expanded to its full footprint via the catalogue's
   per-variant unit mask, rotated by the placement's own rotation.

2. **Free placements** (`is_free=1`) have no grid coords at all, only
   `abs_x/abs_y/abs_z` in metres plus `yaw/pitch/roll`. They cannot be
   grid-matched under any calibration. 12.1% of gold-set placements are
   free, and 37 gold-set maps are majority-free, so ignoring them
   silently writes those maps off. They get an oriented bounding volume
   in world space instead.

Raw placement count is NOT a completeness metric. A short track can
legitimately consist of few anchors, especially when blocks span many
cells, and a low count is a warning to investigate rather than evidence
of under-parsing.
"""
from __future__ import annotations

import collections
import logging
import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from src.catalogue.loader import BlockDef, rotate_offset, rotated_size

_LOG = logging.getLogger(__name__)

# Grid geometry. A cell is 32m square in XZ and 8m tall; the ground row
# is 9 and sits at absolute y = 8m.
CELL_SIZE_M = 32.0
LEVEL_HEIGHT_M = 8.0
GROUND_ROW = 9
GROUND_ABS_Y_M = 8.0

# TM2020 free-fall, MEASURED from the pilot captures rather than assumed:
# vertical acceleration is strongly bimodal, one mode at 0 (on a surface)
# and one at -24 m/s2. That is well away from Earth gravity, so do not
# "correct" it to -9.81.
GRAVITY_MPS2 = -24.0

# Half-width of the band around GRAVITY_MPS2 that counts as ballistic.
# Wide enough to absorb 20Hz differentiation noise, narrow enough to
# exclude a steep downhill (which reads far shallower).
BALLISTIC_TOLERANCE_MPS2 = 8.0


def to_cell(x: float, y: float, z: float) -> tuple[int, int, int]:
    """World metres to block-grid cell."""
    return (
        math.floor(x / CELL_SIZE_M),
        math.floor(GROUND_ROW + (y - GROUND_ABS_Y_M) / LEVEL_HEIGHT_M),
        math.floor(z / CELL_SIZE_M),
    )


@dataclass(frozen=True)
class Placement:
    """One row of ``block_placements``, either grid- or free-placed."""

    index: int
    block_type: str
    variant: int
    is_free: bool
    x: int | None = None
    y: int | None = None
    z: int | None = None
    rotation: int = 0
    abs_x: float | None = None
    abs_y: float | None = None
    abs_z: float | None = None
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0


@dataclass(frozen=True)
class Coverage:
    """Per-sample match outcome."""

    matched_grid: bool
    matched_free: bool
    airborne: bool

    @property
    def matched(self) -> bool:
        return self.matched_grid or self.matched_free


def _pick_variant(block: BlockDef, placement: Placement):
    """Choose the catalogue variant for a placement.

    Prefers the recorded variant index. Falls back across kinds because
    the recorded index is not always present in both, and a missing
    variant would drop the block's footprint entirely.
    """
    kinds = ("ground", "air")
    if placement.y is not None and placement.y > GROUND_ROW:
        kinds = ("air", "ground")
    for kind in kinds:
        v = block.variant(kind, placement.variant)
        if v is not None:
            return v
    for kind in kinds:
        v = block.variant(kind, 0)
        if v is not None:
            return v
    return block.variants[0] if block.variants else None


def grid_footprint_cells(
    placement: Placement,
    catalogue: Mapping[str, BlockDef],
) -> tuple[tuple[int, int, int], ...]:
    """World cells occupied by a grid placement.

    Uses the catalogue's actual per-unit mask when the block is known.
    When it is not, falls back to the rotated bounding box, which
    over-generates rather than under-generates: this is candidate
    generation, and a false candidate is filtered later while a missing
    one is unrecoverable.
    """
    if placement.is_free or placement.x is None:
        return ()
    ox, oy, oz = placement.x, placement.y, placement.z
    block = catalogue.get(placement.block_type)
    if block is None:
        return ((ox, oy, oz),)
    variant = _pick_variant(block, placement)
    if variant is None:
        return ((ox, oy, oz),)

    if variant.units:
        cells = []
        for unit in variant.units:
            dx, dy, dz = rotate_offset(
                unit.offset, placement.rotation, variant.size
            )
            cells.append((ox + dx, oy + dy, oz + dz))
        return tuple(dict.fromkeys(cells))

    # No mask: rotated dimensions as candidate-generation bounds.
    sx, sy, sz = rotated_size(variant.size, placement.rotation)
    return tuple(
        (ox + dx, oy + dy, oz + dz)
        for dx in range(sx)
        for dy in range(sy)
        for dz in range(sz)
    )


@dataclass(frozen=True)
class OrientedBox:
    """World-space oriented bounding volume for a free placement."""

    cx: float
    cy: float
    cz: float
    hx: float
    hy: float
    hz: float
    yaw: float
    placement_index: int

    def contains(self, x: float, y: float, z: float) -> bool:
        dx, dy, dz = x - self.cx, y - self.cy, z - self.cz
        # Undo yaw about the vertical axis; pitch and roll are carried on
        # the placement but folded into the half-extents rather than
        # applied, because a tilted box's AABB is what we want for
        # candidate generation anyway.
        c, s = math.cos(-self.yaw), math.sin(-self.yaw)
        rx = dx * c - dz * s
        rz = dx * s + dz * c
        return abs(rx) <= self.hx and abs(dy) <= self.hy and abs(rz) <= self.hz


def free_placement_box(
    placement: Placement,
    catalogue: Mapping[str, BlockDef],
    *,
    anchor: str = "corner",
    pad_m: float = 0.0,
) -> OrientedBox | None:
    """Oriented bounding volume for a free placement, in world metres.

    ``anchor`` says what ``abs_*`` refers to. TM stores a free block's
    pivot, which is the min corner at its base for most blocks, but that
    is convention rather than documented, so it is a parameter and the
    caller can measure which fits.

    Tilt handling: pitch/roll expand the extents rather than rotating
    the box. For candidate generation an inflated axis-aligned envelope
    is the safe error; a precisely tilted box can exclude the true
    block on a banked or looping section.
    """
    if not placement.is_free or placement.abs_x is None:
        return None
    block = catalogue.get(placement.block_type)
    if block is None:
        # Unknown block: a single-cell envelope so it still generates a
        # candidate instead of vanishing.
        sx = sy = sz = 1
    else:
        variant = _pick_variant(block, placement)
        sx, sy, sz = variant.size if variant is not None else (1, 1, 1)

    ex = sx * CELL_SIZE_M / 2.0
    ey = sy * LEVEL_HEIGHT_M / 2.0
    ez = sz * CELL_SIZE_M / 2.0

    tilt = max(abs(placement.pitch), abs(placement.roll))
    if tilt > 1e-3:
        grow = abs(math.sin(tilt)) * max(ex, ey, ez)
        ex += grow
        ey += grow
        ez += grow

    if anchor == "corner":
        c, s = math.cos(placement.yaw), math.sin(placement.yaw)
        lx, lz = ex, ez
        cx = placement.abs_x + (lx * c - lz * s)
        cz = placement.abs_z + (lx * s + lz * c)
        cy = placement.abs_y + ey
    else:
        cx, cy, cz = placement.abs_x, placement.abs_y, placement.abs_z

    return OrientedBox(
        cx=cx, cy=cy, cz=cz,
        hx=ex + pad_m, hy=ey + pad_m, hz=ez + pad_m,
        yaw=placement.yaw, placement_index=placement.index,
    )


class CandidateIndex:
    """Per-map index answering "which blocks could this sample be on?"."""

    def __init__(
        self,
        placements: Iterable[Placement],
        catalogue: Mapping[str, BlockDef],
        *,
        free_anchor: str = "corner",
        free_pad_m: float = 0.0,
    ) -> None:
        self._cells: dict[tuple[int, int, int], list[int]] = (
            collections.defaultdict(list)
        )
        self._boxes: list[OrientedBox] = []
        self.n_grid = self.n_free = 0
        self.unknown_blocks: set[str] = set()

        for p in placements:
            if p.block_type not in catalogue:
                self.unknown_blocks.add(p.block_type)
            if p.is_free:
                box = free_placement_box(
                    p, catalogue, anchor=free_anchor, pad_m=free_pad_m
                )
                if box is not None:
                    self._boxes.append(box)
                    self.n_free += 1
                continue
            for cell in grid_footprint_cells(p, catalogue):
                self._cells[cell].append(p.index)
            self.n_grid += 1

    @property
    def footprint_cells(self) -> int:
        return len(self._cells)

    def grid_hit(self, x: float, y: float, z: float, *, dy: int = 0) -> bool:
        cx, cy, cz = to_cell(x, y, z)
        return (cx, cy + dy, cz) in self._cells

    def free_hit(self, x: float, y: float, z: float) -> bool:
        return any(b.contains(x, y, z) for b in self._boxes)

    def grid_candidates(
        self, x: float, y: float, z: float, *, dy: int = 0
    ) -> tuple[int, ...]:
        """Placement indices whose footprint holds this position.

        Identity, not membership: coverage only needs to know THAT a
        block is under the car, route inference needs to know WHICH.
        """
        cx, cy, cz = to_cell(x, y, z)
        return tuple(self._cells.get((cx, cy + dy, cz), ()))

    def free_candidates(self, x: float, y: float, z: float) -> tuple[int, ...]:
        return tuple(
            b.placement_index for b in self._boxes if b.contains(x, y, z)
        )


def classify_airborne(samples: Sequence[Mapping[str, float]]) -> list[bool]:
    """Flag ballistic samples from kinematics alone.

    Deliberately independent of block data. Deriving "airborne" from
    "no block matched" would make coverage self-justifying: every
    unmatched sample would be explained away as a jump.
    """
    n = len(samples)
    if n < 3:
        return [False] * n
    out = [False] * n
    lo = GRAVITY_MPS2 - BALLISTIC_TOLERANCE_MPS2
    hi = GRAVITY_MPS2 + BALLISTIC_TOLERANCE_MPS2
    for i in range(1, n - 1):
        dt = (samples[i + 1]["time_ms"] - samples[i - 1]["time_ms"]) / 1000.0
        if dt <= 0:
            continue
        ay = (samples[i + 1]["vy"] - samples[i - 1]["vy"]) / dt
        if lo <= ay <= hi:
            out[i] = True
    return out
