"""Route walker driven by observed placements instead of clip rules.

``ClipWalker`` asks the catalogue whether two blocks may join: do
their route-clips meet across the touching faces? That relation is
real, but it is not the whole game, and treating it as the whole game
is what made the generator refuse things real maps do on every other
track. Three examples straight out of the corpus:

* ``PlatformTechStart`` (clip ``PlatformFCSmallRacing``) sits directly
  beside ``PlatformWaterSpecialTurbo2`` (clip ``PlatformWaterFCSmall``)
  which sits beside ``PlatformTechToDecoWall`` (clip
  ``PlatFormFCSmall``). No two of those clips match. Platform tiles
  butt together and you drive over the seam.
* Map 4269 runs ``PlatformTechTiltTransition1UpLeft`` ->
  ``OpenTechRoadStraight`` -> ``RoadTechCurve1`` in one line: platform,
  open road and road in three consecutive cells.
* ``GateCheckpoint`` has no clips whatsoever. It is a 1x4x1 arch that
  goes *over* the route.

So this walker asks a different question: has the corpus ever placed
B there, next to A, and in how many distinct maps? Legality is
evidence. Everything else — which move to prefer, how to keep the
route from eating itself — is layered on top of that, not baked into
it.

What is still a heuristic, stated plainly: co-occurrence is not
direction. The grammar knows B sits next to A; it does not know you
drive A-then-B rather than B-then-A, and it cannot tell a road's
continuation from the wall beside it. The walker handles that with
occupancy plus a no-U-turn rule, which is enough to build a
non-self-intersecting chain but is not a drivability proof. The
finishability gate remains the thing that decides whether a route
holds up.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from src.catalogue.loader import (
    BlockDef,
    FACE_DELTAS,
    rotate_face,
    rotate_offset,
    rotate_vector,
)
from src.generation.clip_walker import (
    DIRECTIONAL_BLOCK_PATTERNS,
    DIRECTIONAL_WAYPOINTS,
    GATE_FORWARD_LOCAL_FACE,
    GRID_MAX,
    GRID_MIN,
    GROUND_Y,
    MAX_Y,
    Placement,
    RouteDeadEnd,
)
from src.generation.grammar import Move, PlacementGrammar

_LOG = logging.getLogger(__name__)

WALKER_VERSION = "grammar-walker-v0.1"

# A move must be this well attested before the walker will build with
# it. The corpus is 18,935 Stadium2020 maps, so 20 is still a very low
# bar — it exists to drop coincidences (two unrelated blocks that
# happened to land three cells apart), not to police style.
DEFAULT_MIN_MAPS = 20

# Gap moves (a target 2-3 cells away in XZ) need their own, much
# higher bar. A real jump looks exactly like two unrelated blocks that
# happen to sit three cells apart, and the second kind vastly
# outnumbers the first: in a 50-map smoke run, 81% of surviving rows
# were gaps. Breadth is the only thing separating them, so demand a
# lot more of it, and stay off by default.
GAP_MIN_MAPS_FACTOR = 10

Cell = tuple[int, int, int]


@dataclass(frozen=True)
class _Oriented:
    block_id: str
    rotation: int
    cells: tuple[Cell, ...]
    waypoint: str


def _orientations(block: BlockDef) -> list[_Oriented]:
    """Footprints at all four rotations.

    Unlike the clip walker's version this does not require route-clip
    ports, so clipless blocks (every ``Gate*`` arch) are usable.
    """
    variant = block.variant("ground", 0)
    if variant is None:
        return []
    size = variant.size
    return [
        _Oriented(
            block_id=block.block_id,
            rotation=rotation,
            cells=tuple(
                rotate_offset(u.offset, rotation, size) for u in variant.units
            ),
            waypoint=block.waypoint,
        )
        for rotation in range(4)
    ]


def _shift(cell: Cell, delta: Cell) -> Cell:
    return (cell[0] + delta[0], cell[1] + delta[1], cell[2] + delta[2])


def _sign(v: int) -> int:
    return (v > 0) - (v < 0)


def _reverse_offset(move: Move) -> Cell:
    """The move that would undo ``move``, in the TARGET block's frame.

    Negating the offset is not enough. ``move.offset`` lives in the
    source block's frame, and the target sits at
    ``source_rotation + move.rel_rotation`` — so the two frames differ
    by exactly that much. Skip the correction and the walker fails to
    recognise a U-turn after any move that also turns, which is most
    of them.
    """
    back = (-move.offset[0], -move.offset[1], -move.offset[2])
    return rotate_vector(back, (4 - move.rel_rotation) % 4)


def _faces_travel(rotation: int, travel: Cell) -> bool:
    """Does a block at ``rotation`` point along ``travel``?

    Gates and boosters have 180-degree symmetric road but an arrow
    that is not symmetric, so the grammar records both orientations as
    equally real and an unconstrained pick ships half of them
    backwards. This is the same rule the clip walker enforces, with
    the world-space step standing in for the exit port.
    """
    want = FACE_DELTAS[rotate_face(GATE_FORWARD_LOCAL_FACE, rotation)]
    return (want[0], want[2]) == (_sign(travel[0]), _sign(travel[2]))


class GrammarWalker:
    def __init__(
        self,
        catalogue: dict[str, BlockDef],
        grammar: PlacementGrammar,
        pool: list[str],
        seed: int,
        min_maps: int = DEFAULT_MIN_MAPS,
        allow_jumps: bool = False,
        gap_min_maps: int | None = None,
        block_bias: dict[str, float] | None = None,
    ) -> None:
        self._rng = random.Random(seed)
        self._grammar = grammar
        self._min_maps = min_maps
        self._allow_jumps = allow_jumps
        self._gap_min_maps = (
            gap_min_maps if gap_min_maps is not None
            else min_maps * GAP_MIN_MAPS_FACTOR
        )
        self._bias = dict(block_bias or {})
        self._allow = frozenset(pool)

        self._cells: dict[tuple[str, int], tuple[Cell, ...]] = {}
        self._waypoint: dict[str, str] = {}
        for block_id in pool:
            block = catalogue.get(block_id)
            if block is None:
                raise KeyError(f"block not in catalogue: {block_id}")
            for oriented in _orientations(block):
                self._cells[(block_id, oriented.rotation)] = oriented.cells
                self._waypoint[block_id] = oriented.waypoint

        self._starts = [b for b in pool if self._waypoint.get(b) == "Start"]
        self._finishes = [b for b in pool if self._waypoint.get(b) == "Finish"]
        if not self._starts or not self._finishes:
            raise ValueError("pool needs at least one Start and one Finish")

        reachable = sum(1 for b in pool if b in grammar)
        _LOG.info(
            "%s: pool=%d blocks, %d with grammar entries, min_maps=%d",
            WALKER_VERSION, len(pool), reachable, min_maps,
        )
        if not reachable:
            raise ValueError(
                "no block in the pool appears in the grammar; export a "
                "wider slice or lower --min-maps"
            )

    def _bias_for(self, block_id: str) -> float:
        factor = 1.0
        for needle, weight in self._bias.items():
            if needle in block_id:
                factor *= weight
        return factor

    def _weight(self, move: Move) -> float:
        # Breadth, not volume: map_count already answers "how many
        # mappers do this", which is the question. clip_matched is a
        # mild preference, not a gate — those joins are the ones the
        # game itself snaps together.
        w = float(move.map_count) * self._bias_for(move.block)
        return w * (1.5 if move.clip_matched else 1.0)

    def _order(self, moves: list[Move]) -> list[Move]:
        """Weighted order without replacement.

        Same rule as the clip walker's prior handling: weights decide
        what is tried first, never what is allowed. Every candidate
        keeps a turn, so a rare-but-legal move can still close a route
        the common ones dead-end on.
        """
        items = [(m, self._weight(m)) for m in moves]
        out: list[Move] = []
        while items:
            total = sum(w for _, w in items)
            if total <= 0:
                out.extend(m for m, _ in items)
                break
            pick = self._rng.random() * total
            acc = 0.0
            chosen = len(items) - 1
            for i, (_, w) in enumerate(items):
                acc += w
                if pick <= acc:
                    chosen = i
                    break
            out.append(items.pop(chosen)[0])
        return out

    def _candidates(
        self, block_id: str, want: str, incoming: Cell | None,
    ) -> list[Move]:
        moves = self._grammar.successors(
            block_id,
            min_maps=self._min_maps,
            allow=self._allow,
            overlays=False,  # arches go over the route, not along it
            gaps=None if self._allow_jumps else False,
        )
        out = []
        for move in moves:
            if self._waypoint.get(move.block, "None") != want:
                continue
            if incoming is not None and move.offset == incoming:
                # No U-turn: the move that undoes the one just made.
                continue
            if move.is_gap and move.map_count < self._gap_min_maps:
                continue
            out.append(move)
        return out

    def generate(
        self,
        length: int,
        checkpoint_every: int = 12,
        max_expansions: int = 40000,
    ) -> list[Placement]:
        start_id = self._rng.choice(self._starts)
        origin: Cell = (
            GRID_MIN + (GRID_MAX - GRID_MIN) // 2,
            GROUND_Y,
            GRID_MIN + (GRID_MAX - GRID_MIN) // 2,
        )
        start_rot = self._rng.randrange(4)
        placements = [Placement(start_id, *origin, start_rot)]
        occupied: set[Cell] = {
            _shift(origin, c) for c in self._cells[(start_id, start_rot)]
        }
        expansions = 0

        def in_bounds(cells: list[Cell]) -> bool:
            return all(
                GRID_MIN <= c[0] < GRID_MAX
                and GROUND_Y <= c[1] <= MAX_Y
                and GRID_MIN <= c[2] < GRID_MAX
                for c in cells
            )

        def dfs(prev: Placement, incoming: Cell | None, steps: int) -> bool:
            nonlocal expansions
            expansions += 1
            if expansions > max_expansions:
                raise RouteDeadEnd(
                    f"expansion budget exhausted after {expansions} nodes"
                )

            if steps >= length:
                want = "Finish"
            elif (
                checkpoint_every > 0 and steps > 0
                and steps % checkpoint_every == 0
            ):
                want = "Checkpoint"
            else:
                want = "None"

            moves = self._candidates(prev.block_id, want, incoming)
            if not moves and want == "Checkpoint":
                moves = self._candidates(prev.block_id, "None", incoming)

            for move in self._order(moves):
                anchor, rotation = move.apply(
                    (prev.x, prev.y, prev.z), prev.rotation
                )
                cells = self._cells.get((move.block, rotation))
                if cells is None:
                    continue
                footprint = [_shift(anchor, c) for c in cells]
                if not in_bounds(footprint):
                    continue
                if any(c in occupied for c in footprint):
                    continue
                travel = (
                    anchor[0] - prev.x, anchor[1] - prev.y, anchor[2] - prev.z,
                )
                if self._is_directional(move.block) and not _faces_travel(
                    rotation, travel
                ):
                    continue

                nxt = Placement(move.block, *anchor, rotation)
                placements.append(nxt)
                if want == "Finish":
                    return True
                occupied.update(footprint)
                if dfs(nxt, _reverse_offset(move), steps + 1):
                    return True
                placements.pop()
                occupied.difference_update(footprint)
            return False

        if not dfs(placements[0], None, 1):
            raise RouteDeadEnd(
                f"no route of length {length} found from {origin} "
                f"({expansions} expansions)"
            )
        _LOG.info(
            "route closed: %d blocks, %d expansions, walker=%s",
            len(placements), expansions, WALKER_VERSION,
        )
        return placements

    def _is_directional(self, block_id: str) -> bool:
        if self._waypoint.get(block_id) in DIRECTIONAL_WAYPOINTS:
            return True
        return any(p in block_id for p in DIRECTIONAL_BLOCK_PATTERNS)
