"""Clip-matching route walker — first consumer of the block catalogue.

Chains blocks into a start-to-finish route by matching clip ids on
facing block faces, exactly the relation the game itself uses to
join blocks. This replaces the v0.6 unit-cell stepper's guesswork
(name-regex ``connector_hint``) with the catalogue's ground truth,
which is why curves are placeable again.

v0 scope (deliberate, see docs/generation/minimal-ai-generator-v0.md
lineage): flat routes, blocks whose ground-variant XZ footprint is
1x1 (multi-cell XZ placement lands with the full M2 walker). Start,
Checkpoint and Finish gates are taller than one cell but occupy a
single XZ column, so they are inside scope.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from src.catalogue.loader import (
    FACE_DELTAS,
    SIDE_FACES,
    BlockDef,
    opposite_face,
    rotate_face,
)

_LOG = logging.getLogger(__name__)

WALKER_VERSION = "clip-walker-v0.1"

# Stadium 48x48 grid; keep a margin so autoterrain never clips the
# map border.
GRID_MIN = 6
GRID_MAX = 42

# Ground row for road blocks on flat Stadium terrain. Empirical:
# blocks hand-placed in the editor land at y=9 (AutoSave.Map.Gbx,
# 2026-07-25).
GROUND_Y = 9


@dataclass(frozen=True)
class Placement:
    block_id: str
    x: int
    y: int
    z: int
    rotation: int


@dataclass(frozen=True)
class _Oriented:
    """A block at one of its four rotations, as world-face ports."""

    block_id: str
    rotation: int
    # world face -> clip id, for every clipped side face
    ports: tuple[tuple[int, str], ...]
    waypoint: str


class RouteDeadEnd(Exception):
    """Walker exhausted its backtrack budget without closing a route."""


def _orientations(block: BlockDef) -> list[_Oriented]:
    variant = block.variant("ground", 0)
    if variant is None:
        return []
    sx, _, sz = variant.size
    if (sx, sz) != (1, 1):
        # Multi-cell XZ footprints wait for the M2 walker proper.
        return []
    out: list[_Oriented] = []
    local_ports = variant.side_ports()
    if not local_ports:
        return []
    for rotation in range(4):
        ports = tuple(
            (rotate_face(p.face, rotation), p.clip_id) for p in local_ports
        )
        out.append(
            _Oriented(
                block_id=block.block_id,
                rotation=rotation,
                ports=ports,
                waypoint=block.waypoint,
            )
        )
    return out


class ClipWalker:
    """Randomized clip-matched route generation over 1x1 road blocks."""

    def __init__(
        self,
        catalogue: dict[str, BlockDef],
        allowed_ids: list[str],
        seed: int,
    ) -> None:
        self._rng = random.Random(seed)
        self._orient: list[_Oriented] = []
        for block_id in allowed_ids:
            block = catalogue.get(block_id)
            if block is None:
                raise KeyError(f"block not in catalogue: {block_id}")
            variants = _orientations(block)
            if not variants:
                _LOG.warning(
                    "block %s has no usable 1x1 ground variant; skipped",
                    block_id,
                )
            self._orient.extend(variants)

        self._starts = [o for o in self._orient if o.waypoint == "Start"]
        self._finishes = [o for o in self._orient if o.waypoint == "Finish"]
        self._checkpoints = [o for o in self._orient if o.waypoint == "Checkpoint"]
        self._plain = [o for o in self._orient if o.waypoint == "None"]
        if not self._starts or not self._finishes:
            raise ValueError("allowed set needs at least one Start and one Finish")

    def generate(
        self,
        length: int,
        checkpoint_every: int = 12,
        max_expansions: int = 20000,
    ) -> list[Placement]:
        """Build a route of roughly ``length`` road blocks.

        Depth-first with backtracking. Every consecutive pair is
        clip-matched by construction; the finish is only placed on a
        matching open port, so a returned route is closed end-to-end.
        """
        start = self._rng.choice(self._starts)
        # The start gate's single port is its exit; face the route
        # into the grid's interior from a fixed spawn cell.
        origin = (GRID_MIN + (GRID_MAX - GRID_MIN) // 2,
                  GROUND_Y,
                  GRID_MAX - 4)

        placements = [Placement(start.block_id, *origin, start.rotation)]
        occupied = {(origin[0], origin[2])}
        exit_face, exit_clip = start.ports[0]

        expansions = 0
        route: list[tuple[Placement, int, str]] = []

        def cell_ahead(cell: tuple[int, int, int], face: int) -> tuple[int, int, int]:
            dx, dy, dz = FACE_DELTAS[face]
            return (cell[0] + dx, cell[1] + dy, cell[2] + dz)

        def in_bounds(cell: tuple[int, int, int]) -> bool:
            return GRID_MIN <= cell[0] < GRID_MAX and GRID_MIN <= cell[2] < GRID_MAX

        def dfs(cell: tuple[int, int, int], face: int, clip: str, steps: int) -> bool:
            nonlocal expansions
            expansions += 1
            if expansions > max_expansions:
                raise RouteDeadEnd(
                    f"expansion budget exhausted after {expansions} nodes"
                )
            target = cell_ahead(cell, face)
            if not in_bounds(target) or (target[0], target[2]) in occupied:
                return False
            entry_face = opposite_face(face)

            want_finish = steps >= length
            want_checkpoint = (
                checkpoint_every > 0
                and steps > 0
                and steps % checkpoint_every == 0
            )
            if want_finish:
                pool = list(self._finishes)
            elif want_checkpoint:
                pool = list(self._checkpoints) or list(self._plain)
            else:
                pool = list(self._plain)
            self._rng.shuffle(pool)

            for cand in pool:
                entry = [p for p in cand.ports if p == (entry_face, clip)]
                if not entry:
                    continue
                exits = [p for p in cand.ports if p != (entry_face, clip)]
                if cand.waypoint == "Finish":
                    if exits:
                        continue  # a finish must terminate the route
                    placements.append(Placement(cand.block_id, *target, cand.rotation))
                    return True
                if len(exits) != 1:
                    continue  # v0 walks linear routes only
                placements.append(Placement(cand.block_id, *target, cand.rotation))
                occupied.add((target[0], target[2]))
                next_face, next_clip = exits[0]
                if dfs(target, next_face, next_clip, steps + 1):
                    return True
                placements.pop()
                occupied.discard((target[0], target[2]))
            return False

        if not dfs(origin, exit_face, exit_clip, 1):
            raise RouteDeadEnd(
                f"no route of length {length} found from {origin} "
                f"({expansions} expansions)"
            )
        _LOG.info(
            "route closed: %d blocks, %d expansions, seed walker=%s",
            len(placements), expansions, WALKER_VERSION,
        )
        return placements
