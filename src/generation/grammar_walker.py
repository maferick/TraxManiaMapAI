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
* ``GateCheckpoint`` has no clips whatsoever. It is a 1x4x1 arch, and
  the corpus mounts it on a pillar column — it shares its cell with
  ``StructurePillar`` in 353 maps — so nothing about how it attaches
  is expressible as a clip meeting.

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

import collections
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

# How often a racing line may pass over ground it has already used.
#
# Measured over 201 corpus maps (route blocks only, walls and pillars
# excluded): 95.2% of XZ columns under a racing line carry exactly ONE
# route level, 4.2% carry two — a bridge crossing over an earlier
# section — 0.5% three, 0.1% four. A real route almost never stacks on
# itself; median distinct columns per route block is 0.98.
#
# Left unconstrained the walker produced 48% of its columns carrying
# 2-4 levels, crammed into a 20x5 patch. Every block was connected and
# no cell overlapped, and it still read as a heap of slabs rather than
# a track, because nothing was pushing it to go anywhere. The cap is
# the hard limit; the penalty reproduces the corpus's rate.
MAX_LEVELS_PER_COLUMN = 2
COLUMN_REUSE_PENALTY = 0.05

# Clip agreement is not the universal join rule — that is the whole
# point of this walker — but where it DOES apply it is the game's own
# notion of two road surfaces meeting, and ignoring it has a visible
# cost: TM2020 draws a yellow-and-black dead-end barrier at every road
# end that is not joined. A route with 27% non-clip steps came out
# with 27 barriers scattered through it.
#
# So clip-matched moves are tried FIRST, exhaustively, and a non-clip
# move is only reached when none of them works. That keeps the cases
# the corpus proves are real (PlatformTechStart -> PlatformTechBase is
# non-clip in 429 maps) without using them where the game would rather
# have snapped two blocks together. Non-clip moves also carry a higher
# evidence bar, since a weak one is usually just proximity.
NO_CLIP_MIN_MAPS_FACTOR = 5

# How hard to steer toward a requested surface the route has not
# reached yet. Weighting is by corpus breadth, and the popular blocks
# dominate by orders of magnitude — RoadTechStraight is attested in 746
# maps against a few dozen for a surface-transition block — so "grass
# and dirt" produced 88 tech, 13 dirt and no grass at all. This has to
# outweigh that gap, and it stops applying the moment the surface is
# reached, so it steers the route rather than filling it.
UNVISITED_FAMILY_BOOST = 5000.0

# Weight multiplier for a move the corpus has actually been observed to
# make NEXT, given the block before the current one.
#
# A bigram walker reproduces the marginal: it knows Straight is common
# after Straight, but not that Straight x3 is a pattern in 274 maps or
# that SpecialTurbo2 comes in RUNS of three in 83. Multiplying by the
# triple's own map_count lets an attested continuation of a run beat an
# unrelated block that merely happens to be a popular neighbour.
#
# MEASURED AND DEFAULTED OFF. This did not work, and the number is 0
# so that the mechanism stays available without being on by mistake.
#
# Corpus baseline over 145 maps: median 21 distinct route block types
# per map, most-used block 29% of the line. Generated routes already
# sit at 14 distinct / 0.31 — too repetitive BEFORE any prior. Adding
# the sequence prior moved both the wrong way at every weight tried:
#
#     bigram only   14 distinct   0.31 top-block share
#     w = 0.5       14            0.33
#     w = 1.5       13            0.36
#     w = 3.0       13            0.40
#     w = 8.0       11            0.40
#
# The reason is structural, not a tuning failure: the strongest triples
# ARE same-block runs (Straight x3 in 274 maps, SpecialTurbo2 x3 in
# 83), so rewarding attested runs rewards repetition. A real map's
# variety comes from many different local patterns across the whole
# line, which a greedy per-step multiplier cannot express.
#
# The triples remain worth having — they are the only ordered evidence
# in the project — but consuming them needs something that shapes the
# whole route, not a step-local weight. Left for a planner.
SEQUENCE_WEIGHT = 0.0

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


def _POOL_PREFIXES(pool) -> set[str]:
    """Surface prefixes present in a pool, for spotting `<A>To<B>` blocks."""
    known = ("RoadTech", "RoadDirt", "RoadBump", "RoadIce", "RoadWater",
             "PlatformTech", "PlatformDirt", "PlatformIce",
             "PlatformGrass", "PlatformPlastic", "PlatformWater")
    return {p for p in known if any(b.startswith(p) for b in pool)}


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


_NEIGHBOURS = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))


def _touches(source: set[Cell], target: list[Cell]) -> bool:
    """Do the two footprints share a FACE, not just an edge or corner?

    The grammar records every nearby pair, including the ones that sit
    kitty-corner — a curve and the block diagonally off it are as real
    a pair as a straight and the block ahead of it. Chained as route
    steps those produce exactly what the first driven map looked like:
    slabs meeting at their corners with nothing to drive across.

    Face contact is the right test rather than "the step must be
    axis-aligned", because a multi-cell block's anchor-to-anchor step
    is legitimately diagonal — a 2x2 curve hands off at ``(1, 0, 2)``.
    It also rules out bogus height changes for free: a 1x1 block cannot
    reach ``(0, +1, +1)``, while a 1x2x1 slope can, which is precisely
    the difference between a ramp and a floating slab.
    """
    for cx, cy, cz in target:
        for dx, dy, dz in _NEIGHBOURS:
            if (cx + dx, cy + dy, cz + dz) in source:
                return True
    return False


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
        route_only: bool = True,
        require_prefixes: list[str] | None = None,
        route_model=None,
    ) -> None:
        self._seed = seed
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
        # Surfaces the request named, which the route must actually
        # visit. Asking for "grass and dirt" and getting neither is a
        # failure even if the pool contained both: weighting is by
        # corpus breadth, and RoadTechStraight is attested in 746 maps
        # against a few dozen for the PlatformGrassToRoadTech bridge,
        # so a plain weighted walk never crosses over.
        self._require = tuple(require_prefixes or ())
        # Ordered three-block runs mined from the corpus. Optional: the
        # walker still works on pairwise evidence alone.
        self._model = route_model

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

        if route_only:
            # Without this the walker builds routes out of WALLS. Not a
            # hypothetical: the first run over the real corpus put 44
            # PlatformPlasticWall* blocks into a 101-block route,
            # because a wall beside the road is as strong a neighbour
            # as the road ahead and co-occurrence cannot tell them
            # apart. Growing the vocabulary out from the waypoints can
            # (see PlacementGrammar.route_vocabulary).
            seeds = [
                b for b in pool
                if self._waypoint.get(b) in (
                    "Start", "Finish", "Checkpoint", "StartFinish")
            ]
            # Surface-transition blocks must be seeded, not discovered.
            # They are the ONLY way two surfaces meet, and they are far
            # too rare to survive a top-N cut: a grass+dirt pool grew a
            # 129-block vocabulary containing 42 grass blocks and zero
            # bridges, so no route could ever cross and "grass and dirt"
            # was unbuildable. The `<A>To<B>` naming is not a guess — it
            # was verified exhaustively against the catalogue when the
            # transition table was mapped.
            bridges = sorted(
                b for b in pool
                if any(f"To{p}" in b for p in _POOL_PREFIXES(pool))
            )
            if bridges:
                _LOG.info(
                    "%s: seeding %d surface-transition blocks",
                    WALKER_VERSION, len(bridges),
                )
            self._allow = grammar.route_vocabulary(
                seeds + bridges, self._allow, min_maps=min_maps
            )

        reachable = sum(1 for b in self._allow if b in grammar)
        _LOG.info(
            "%s: pool=%d blocks (%d usable, %d with grammar entries), "
            "min_maps=%d",
            WALKER_VERSION, len(pool), len(self._allow), reachable, min_maps,
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

    def _order(self, scored: list[tuple]) -> list:
        """Try every clip-matched move before any non-clip one.

        Within each tier, weighted order without replacement — the same
        rule as the clip walker's priors: weights decide what is tried
        first, never what is allowed. Every candidate keeps a turn, so
        a rare-but-legal move can still close a route the common ones
        dead-end on.

        The tiering is what stops the map filling with the game's
        dead-end barriers, and it costs nothing in reach: the non-clip
        tier is still there when the clip-matched one runs out.
        """
        clip = [s for s in scored if s[0][0].clip_matched]
        rest = [s for s in scored if not s[0][0].clip_matched]
        return self._shuffle(clip) + self._shuffle(rest)

    def _shuffle(self, scored: list[tuple]) -> list:
        items = list(scored)
        out: list = []
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
            overlays=False,  # same column, so never a continuation
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

        # If this block has any clip-matched continuation at all, use
        # only those: the game snaps them together, and anything else
        # leaves a dead-end barrier. If it has none, the block is
        # structurally a gate — PlatformPlasticStart has nine
        # successors and not one of them clips — and non-clip is simply
        # how it attaches.
        clipped = [m for m in out if m.clip_matched]
        if clipped:
            return clipped
        return [
            m for m in out
            if m.map_count >= self._min_maps * NO_CLIP_MIN_MAPS_FACTOR
        ] or out

    def generate(
        self,
        length: int,
        checkpoint_every: int = 12,
        max_expansions: int = 40000,
        attempts: int = 24,
    ) -> list[Placement]:
        """Build a route, restarting rather than backtracking forever.

        Depth-first search on a pool this constrained thrashes: it digs
        to within a few blocks of the target, dead-ends, and unwinds one
        step at a time. Measured on a dirt+plastic pool, four of twelve
        seeds blew a 40k-node budget and one still failed at 400k — yet
        the same seeds close immediately from a different start. So
        restart from a fresh draw instead of raising the budget.

        An attempt is also rejected if the finished route never reached
        one of ``require_prefixes`` — asking for grass and dirt and
        getting neither is a failure, and the boost that steers toward
        an unvisited surface is greedy enough to miss on its own.

        Each attempt reseeds deterministically from the walker's seed,
        so a seed still maps to exactly one map.
        """
        last: RouteDeadEnd | None = None
        for attempt in range(attempts):
            self._rng = random.Random(f"{self._seed}:{attempt}")
            try:
                route = self._attempt(length, checkpoint_every, max_expansions)
                missing = [
                    p for p in self._require
                    if not any(pl.block_id.startswith(p) for pl in route)
                ]
                if missing:
                    # The boost is greedy: it can only steer toward a
                    # requested surface when a candidate at the current
                    # node already belongs to one, and reaching a
                    # surface-transition block can take several steps.
                    # So verify the finished route and retry rather than
                    # quietly shipping a map missing what was asked for.
                    raise RouteDeadEnd(
                        f"route reached none of {missing}"
                    )
                return route
            except RouteDeadEnd as exc:
                last = exc
                _LOG.debug(
                    "%s: attempt %d/%d failed (%s)",
                    WALKER_VERSION, attempt + 1, attempts, exc,
                )
        raise RouteDeadEnd(
            f"no route of length {length} after {attempts} attempts: {last}"
        )

    def _attempt(
        self,
        length: int,
        checkpoint_every: int,
        max_expansions: int,
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
        # How many route levels stand over each XZ column. Keeps the
        # line from coiling into a pile — see MAX_LEVELS_PER_COLUMN.
        columns: collections.Counter = collections.Counter(
            (c[0], c[2]) for c in occupied
        )
        # Requested surfaces the route has already reached.
        visited: set[str] = {
            p for p in self._require if start_id.startswith(p)
        }
        expansions = 0

        def in_bounds(cells: list[Cell]) -> bool:
            return all(
                GRID_MIN <= c[0] < GRID_MAX
                and GROUND_Y <= c[1] <= MAX_Y
                and GRID_MIN <= c[2] < GRID_MAX
                for c in cells
            )

        def dfs(
            prev: Placement, incoming: Cell | None, steps: int,
            before: str | None = None,
        ) -> bool:
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

            here = (prev.x, prev.y, prev.z)
            source = {
                _shift(here, c)
                for c in self._cells[(prev.block_id, prev.rotation)]
            }

            scored: list[tuple] = []
            for move in moves:
                anchor, rotation = move.apply(here, prev.rotation)
                cells = self._cells.get((move.block, rotation))
                if cells is None:
                    continue
                footprint = [_shift(anchor, c) for c in cells]
                if not in_bounds(footprint):
                    continue
                if any(c in occupied for c in footprint):
                    continue
                # A jump is defined by NOT touching; everything else has
                # to, or the route is a chain of corner-to-corner slabs.
                if move.is_gap:
                    if _touches(source, footprint):
                        continue
                elif not _touches(source, footprint):
                    continue
                travel = (
                    anchor[0] - prev.x, anchor[1] - prev.y, anchor[2] - prev.z,
                )
                if self._is_directional(move.block) and not _faces_travel(
                    rotation, travel
                ):
                    continue
                cols = {(c[0], c[2]) for c in footprint}
                if any(
                    columns[col] >= MAX_LEVELS_PER_COLUMN for col in cols
                ):
                    continue
                weight = self._weight(move)
                if self._model is not None:
                    # NORMALISED share of the context, never the raw
                    # map_count — see RouteModel.sequence_score.
                    score = self._model.sequence_score(
                        before, prev.block_id, move.block,
                        move.offset, move.rel_rotation,
                    )
                    if score:
                        weight *= 1.0 + SEQUENCE_WEIGHT * score
                if any(columns[col] for col in cols):
                    # Passing back over ground the line already covers.
                    # Legal — 4.2% of corpus columns are a bridge — but
                    # it should stay that rare.
                    weight *= COLUMN_REUSE_PENALTY
                if any(
                    move.block.startswith(p) for p in self._require
                    if p not in visited
                ):
                    # A requested surface the route has not reached yet.
                    # Big enough to beat a common move's raw map_count.
                    weight *= UNVISITED_FAMILY_BOOST
                scored.append(((move, anchor, rotation, footprint, cols), weight))

            for move, anchor, rotation, footprint, cols in self._order(scored):
                nxt = Placement(move.block, *anchor, rotation)
                placements.append(nxt)
                if want == "Finish":
                    return True
                occupied.update(footprint)
                columns.update(cols)
                reached = {
                    p for p in self._require
                    if p not in visited and move.block.startswith(p)
                }
                visited.update(reached)
                if dfs(
                    nxt, _reverse_offset(move), steps + 1, prev.block_id,
                ):
                    return True
                placements.pop()
                occupied.difference_update(footprint)
                columns.subtract(cols)
                visited.difference_update(reached)
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
