"""Clip-matching route walker — first consumer of the block catalogue.

Chains blocks into a start-to-finish route by matching clip ids on
facing block faces, exactly the relation the game itself uses to
join blocks. This replaces the v0.6 unit-cell stepper's guesswork
(name-regex ``connector_hint``) with the catalogue's ground truth.

v0.2 scope: multi-cell XZ footprints (Curve2+, chicanes) and
elevation via flat-ended slope blocks (SlopeBase/SlopeBase2). The
route is a linear chain: every non-terminal block must expose
exactly two route-clip ports. Branch pieces wait for a route
planner that can close loops.

Route ports are restricted to an explicit clip allowlist: slope and
gate blocks carry additional wall-surface clips (``TrackWallVFC``
etc.) that join scenery, not road — treating those as exits would
route the car into a wall face.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from src.catalogue.loader import (
    FACE_DELTAS,
    FACE_NORTH,
    BlockDef,
    opposite_face,
    rotate_face,
    rotate_offset,
)
from src.generation.priors import FacePriors

_LOG = logging.getLogger(__name__)

WALKER_VERSION = "clip-walker-v0.3"

# Stadium 48x48 grid; keep a margin so autoterrain never clips the
# map border.
GRID_MIN = 6
GRID_MAX = 42

# Ground row for road blocks on flat Stadium terrain. Empirical:
# blocks hand-placed in the editor land at y=9 (AutoSave.Map.Gbx,
# 2026-07-25).
GROUND_Y = 9

# Highest anchor row the walker will climb to. Stadium's grid is 40
# tall; this is a route-sanity bound, not a game limit.
MAX_Y = GROUND_Y + 10

# A gate's arrow points out of its local NORTH face. Empirical:
# seed-42 run had three RoadTechCheckpoints; the two satisfying
# rotate_face(north, d) == travel direction rendered correct arrows,
# the one violating it rendered backwards (user-arbitrated,
# 2026-07-25). Road-symmetric gates mesh at d and d+2 equally, so
# alignment must be enforced, not left to candidate order.
GATE_FORWARD_LOCAL_FACE = FACE_NORTH

# Clips the route is allowed to travel over. Everything else on a
# block (wall faces, slope-surface joints, diagonal road families)
# is invisible to the walker in v0.2.
DEFAULT_ROUTE_CLIPS = frozenset({"RoadTechFC"})

Cell = tuple[int, int, int]


@dataclass(frozen=True)
class Placement:
    block_id: str
    x: int
    y: int
    z: int
    rotation: int
    # Block variant index; None = emitter default. Only the support
    # pass sets this (the game's pillar stack uses variant to pick
    # foot / transition / shaft meshes).
    variant: int | None = None


@dataclass(frozen=True)
class _Port:
    cell: Cell  # rotated unit offset relative to the anchor
    face: int   # world face
    clip: str


@dataclass(frozen=True)
class _Oriented:
    """A block at one of its four rotations."""

    block_id: str
    rotation: int
    cells: tuple[Cell, ...]      # rotated unit offsets (occupancy)
    ports: tuple[_Port, ...]     # route-clip ports only
    waypoint: str


class RouteDeadEnd(Exception):
    """Walker exhausted its backtrack budget without closing a route."""


def _orientations(
    block: BlockDef, route_clips: frozenset[str]
) -> list[_Oriented]:
    variant = block.variant("ground", 0)
    if variant is None:
        return []
    size = variant.size
    local_ports = [
        p for p in variant.side_ports() if p.clip_id in route_clips
    ]
    if not local_ports:
        return []
    out: list[_Oriented] = []
    for rotation in range(4):
        cells = tuple(
            rotate_offset(u.offset, rotation, size) for u in variant.units
        )
        ports = tuple(
            _Port(
                cell=rotate_offset(p.offset, rotation, size),
                face=rotate_face(p.face, rotation),
                clip=p.clip_id,
            )
            for p in local_ports
        )
        out.append(
            _Oriented(
                block_id=block.block_id,
                rotation=rotation,
                cells=cells,
                ports=ports,
                waypoint=block.waypoint,
            )
        )
    return out


def _shift(cell: Cell, delta: Cell) -> Cell:
    return (cell[0] + delta[0], cell[1] + delta[1], cell[2] + delta[2])


def _sub(a: Cell, b: Cell) -> Cell:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


class ClipWalker:
    """Randomized clip-matched route generation over catalogue blocks."""

    def __init__(
        self,
        catalogue: dict[str, BlockDef],
        allowed_ids: list[str],
        seed: int,
        route_clips: frozenset[str] = DEFAULT_ROUTE_CLIPS,
        priors: FacePriors | None = None,
        block_bias: dict[str, float] | None = None,
    ) -> None:
        self._rng = random.Random(seed)
        self._priors = priors
        # Style bias: substring of a block id -> weight multiplier.
        # Multiplies the corpus prior, so it shifts WHICH legal block
        # gets tried first without ever making an illegal one legal.
        # A 0.0 weight effectively bans a block.
        self._bias = dict(block_bias or {})
        self._orient: list[_Oriented] = []
        for block_id in allowed_ids:
            block = catalogue.get(block_id)
            if block is None:
                raise KeyError(f"block not in catalogue: {block_id}")
            variants = _orientations(block, route_clips)
            if not variants:
                _LOG.warning(
                    "block %s exposes no route-clip ports; skipped", block_id
                )
            self._orient.extend(variants)

        def usable(o: _Oriented, n_ports: int) -> bool:
            return len(o.ports) == n_ports

        self._starts = [o for o in self._orient
                        if o.waypoint == "Start" and usable(o, 1)]
        self._finishes = [o for o in self._orient
                          if o.waypoint == "Finish" and usable(o, 1)]
        self._checkpoints = [o for o in self._orient
                             if o.waypoint == "Checkpoint" and usable(o, 2)]
        self._plain = [o for o in self._orient
                       if o.waypoint == "None" and usable(o, 2)]
        dropped = [o.block_id for o in self._orient
                   if o.waypoint == "None" and not usable(o, 2)]
        if dropped:
            _LOG.warning(
                "non-linear blocks dropped (branch support pending): %s",
                sorted(set(dropped)),
            )
        if not self._starts or not self._finishes:
            raise ValueError("allowed set needs at least one Start and one Finish")

    def _bias_for(self, block_id: str) -> float:
        factor = 1.0
        for needle, weight in self._bias.items():
            if needle in block_id:
                factor *= weight
        return factor

    def _weight(self, prev: Placement, cand: "_Oriented", clip: str) -> float:
        base = 1.0 if self._priors is None else self._priors.weight(
            prev.block_id, prev.rotation, cand.block_id, cand.rotation, clip,
        )
        return base * self._bias_for(cand.block_id)

    def _order_candidates(
        self,
        pool: list[_Oriented],
        prev: Placement,
        clip: str,
    ) -> list[_Oriented]:
        """Candidate try-order for the DFS.

        Without priors: uniform shuffle (v0.2 behaviour). With priors:
        weighted order without replacement — corpus-frequent
        continuations are tried first, but every clip-legal candidate
        keeps a non-zero chance (add-one smoothing in FacePriors), so
        priors bias style without gating validity.
        """
        if self._priors is None and not self._bias:
            self._rng.shuffle(pool)
            return pool
        items = [
            (cand, self._weight(prev, cand, clip))
            for cand in pool
        ]
        ordered: list[_Oriented] = []
        while items:
            total = sum(w for _, w in items)
            pick = self._rng.random() * total
            acc = 0.0
            chosen = len(items) - 1
            for i, (_, w) in enumerate(items):
                acc += w
                if pick <= acc:
                    chosen = i
                    break
            ordered.append(items.pop(chosen)[0])
        return ordered

    def generate(
        self,
        length: int,
        checkpoint_every: int = 12,
        max_expansions: int = 40000,
    ) -> list[Placement]:
        """Build a route of roughly ``length`` blocks.

        Depth-first with backtracking. Every consecutive pair is
        clip-matched by construction; the finish is only placed on a
        matching open port, so a returned route is closed end-to-end.
        """
        start = self._rng.choice(self._starts)
        origin: Cell = (
            GRID_MIN + (GRID_MAX - GRID_MIN) // 2,
            GROUND_Y,
            GRID_MIN + (GRID_MAX - GRID_MIN) // 2,
        )

        placements = [Placement(start.block_id, *origin, start.rotation)]
        occupied: set[Cell] = {_shift(origin, c) for c in start.cells}
        port = start.ports[0]
        open_cell = _shift(origin, port.cell)

        expansions = 0

        def in_bounds(cells: list[Cell]) -> bool:
            return all(
                GRID_MIN <= c[0] < GRID_MAX
                and GROUND_Y <= c[1] <= MAX_Y
                and GRID_MIN <= c[2] < GRID_MAX
                for c in cells
            )

        def dfs(wcell: Cell, wface: int, clip: str, steps: int) -> bool:
            nonlocal expansions
            expansions += 1
            if expansions > max_expansions:
                raise RouteDeadEnd(
                    f"expansion budget exhausted after {expansions} nodes"
                )
            target = _shift(wcell, FACE_DELTAS[wface])
            entry_face = opposite_face(wface)

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
            pool = self._order_candidates(pool, placements[-1], clip)

            for cand in pool:
                for entry in cand.ports:
                    if entry.face != entry_face or entry.clip != clip:
                        continue
                    anchor = _sub(target, entry.cell)
                    footprint = [_shift(anchor, c) for c in cand.cells]
                    if not in_bounds(footprint):
                        continue
                    if any(c in occupied for c in footprint):
                        continue

                    exits = [p for p in cand.ports if p is not entry]
                    if cand.waypoint == "Finish":
                        if exits:
                            continue
                        placements.append(
                            Placement(cand.block_id, *anchor, cand.rotation)
                        )
                        return True
                    if len(exits) != 1:
                        continue
                    if cand.waypoint in ("Checkpoint", "StartFinish"):
                        # Gates must face the direction of travel.
                        forward = rotate_face(
                            GATE_FORWARD_LOCAL_FACE, cand.rotation
                        )
                        if forward != exits[0].face:
                            continue

                    placements.append(
                        Placement(cand.block_id, *anchor, cand.rotation)
                    )
                    occupied.update(footprint)
                    nxt = exits[0]
                    if dfs(
                        _shift(anchor, nxt.cell), nxt.face, nxt.clip, steps + 1
                    ):
                        return True
                    placements.pop()
                    occupied.difference_update(footprint)
            return False

        if not dfs(open_cell, port.face, port.clip, 1):
            raise RouteDeadEnd(
                f"no route of length {length} found from {origin} "
                f"({expansions} expansions)"
            )
        _LOG.info(
            "route closed: %d blocks, %d expansions, walker=%s",
            len(placements), expansions, WALKER_VERSION,
        )
        return placements
