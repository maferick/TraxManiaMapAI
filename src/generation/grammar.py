"""Generation-time view of the mined placement grammar.

Loads the JSON exported by ``export-placement-grammar`` and answers
the only question the walker needs: *given this block at this
rotation, what has the corpus actually put next to it, and where?*

A move is expressed in block A's own frame, so applying it is pure
arithmetic — rotate the offset by A's rotation, add it to A's anchor,
add the relative rotation. No clip lookup, no port matching. That is
the point: clip matching is one way blocks join, and the corpus
contains several others.

Weighting uses ``map_count`` (how many distinct maps contain the
pattern) rather than ``pair_count`` (how many times it occurs). One
map with a forty-block straight run must not outvote forty maps that
each use a pattern once — the same breadth-over-volume rule
``FacePriors`` already follows.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from src.catalogue.loader import rotate_vector

_LOG = logging.getLogger(__name__)

SCHEMA = "placement_grammar_v1"


@dataclass(frozen=True)
class Move:
    """One observed placement of ``block`` relative to a source block."""

    block: str
    offset: tuple[int, int, int]  # in the source block's frame
    rel_rotation: int
    map_count: int
    pair_count: int
    clip_matched: bool

    @property
    def is_overlay(self) -> bool:
        """Shares the source's cell in XZ.

        Two blocks in one column. The corpus does this for gate arches
        on pillar stacks (``GateCheckpoint`` shares its cell with
        ``StructurePillar`` in 353 maps) — never a route continuation,
        which is why the walker excludes these.
        """
        return self.offset[0] == 0 and self.offset[2] == 0

    @property
    def is_gap(self) -> bool:
        """Separated in XZ — a jump, or two unrelated nearby blocks."""
        return max(abs(self.offset[0]), abs(self.offset[2])) > 1

    def apply(
        self, anchor: tuple[int, int, int], rotation: int
    ) -> tuple[tuple[int, int, int], int]:
        """Where and how ``block`` lands, given the source placement."""
        dx, dy, dz = rotate_vector(self.offset, rotation)
        return (
            (anchor[0] + dx, anchor[1] + dy, anchor[2] + dz),
            (rotation + self.rel_rotation) % 4,
        )


class PlacementGrammar:
    def __init__(self, moves: dict[str, tuple[Move, ...]], environment: str):
        self._moves = moves
        self.environment = environment

    @classmethod
    def from_json(cls, path: str | Path) -> "PlacementGrammar":
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        if doc.get("schema") != SCHEMA:
            raise ValueError(f"unsupported grammar schema: {doc.get('schema')!r}")
        offsets = [tuple(o) for o in doc["offsets"]]
        moves: dict[str, tuple[Move, ...]] = {}
        for block_a, rows in doc["rules"].items():
            moves[block_a] = tuple(
                Move(
                    block=str(b), offset=offsets[int(oid)],
                    rel_rotation=int(rel), map_count=int(maps),
                    pair_count=int(pairs), clip_matched=bool(clip),
                )
                for b, oid, rel, maps, pairs, clip in rows
            )
        _LOG.info(
            "placement grammar: %d source blocks, %d moves (%s)",
            len(moves), sum(len(m) for m in moves.values()),
            doc.get("environment", "?"),
        )
        return cls(moves, str(doc.get("environment", "")))

    def successors(
        self,
        block_a: str,
        *,
        min_maps: int = 1,
        allow: frozenset[str] | None = None,
        overlays: bool | None = None,
        gaps: bool | None = None,
    ) -> tuple[Move, ...]:
        """Observed placements around ``block_a``, strongest first.

        ``allow`` restricts the target block, which is how a run keeps
        to a chosen vocabulary. ``overlays`` / ``gaps`` select move
        kinds: ``None`` keeps them, ``False`` drops them, ``True``
        keeps only them.
        """
        out = []
        for move in self._moves.get(block_a, ()):
            if move.map_count < min_maps:
                continue
            if allow is not None and move.block not in allow:
                continue
            if overlays is not None and move.is_overlay != overlays:
                continue
            if gaps is not None and move.is_gap != gaps:
                continue
            out.append(move)
        return tuple(out)

    def route_vocabulary(
        self,
        seeds: list[str],
        allow: frozenset[str],
        *,
        min_maps: int = 20,
        rounds: int = 4,
        per_block: int = 6,
    ) -> frozenset[str]:
        """Blocks the corpus puts ON the racing line, grown from waypoints.

        Co-occurrence cannot tell a road's continuation from the wall
        beside it, and left to itself the walker builds routes out of
        walls — 44 of 101 blocks in the first real run. The corpus
        does separate them, though, once you ask the right block:

        * ``PlatformPlasticCheckpoint`` — unambiguously on the racing
          line — has ``PlatformPlasticBase`` as its neighbour and no
          wall anywhere in its top rows.
        * ``PlatformPlasticWallStraight`` has only more of itself, and
          at offsets ``(0, ±1, 0)`` and ``(0, ±2, 0)``: walls stack
          vertically, road does not.

        So start from the blocks that are certainly route — the
        waypoints — and keep each block's ``per_block`` strongest
        DISTINCT neighbours. Distinct matters: a row is one
        (target, offset, rotation), and ``PlatformPlasticBase``'s
        twelve strongest rows are all ``PlatformPlasticBase`` at twelve
        different offsets. A row-based cut therefore never reached
        ``PlatformPlasticCurve1`` — 31 plastic corner blocks exist and
        not one was buildable, which is why plastic sections came out
        as square-cornered slabs.

        Walls are no longer this function's problem. The walker's
        clip-first rule handles them: a wall carries
        ``PlatFormWallStraightFC`` and a tile carries
        ``PlatFormFCSmall``, so they never clip and the walker never
        reaches for one. Measured over twelve routes at
        ``per_block=6``: 1212 blocks placed, zero plain wall blocks,
        35 plastic curves used.
        """
        vocab = set(seeds)
        frontier = set(seeds)
        for _ in range(rounds):
            grown: set[str] = set()
            for block in frontier:
                moves = self.successors(
                    block, min_maps=min_maps, allow=allow,
                    overlays=False, gaps=False,
                )
                # Count DISTINCT targets, not rows. A block's rows are
                # one per (target, offset, rotation), and a platform
                # tile's twelve strongest are all the same tile at
                # twelve different offsets — so a row-based cut never
                # reached PlatformPlasticCurve1 at all, and plastic
                # sections came out as square-cornered slabs.
                seen: set[str] = set()
                for move in moves:
                    if move.block in seen:
                        continue
                    seen.add(move.block)
                    if len(seen) > per_block:
                        break
                    if move.block not in vocab:
                        grown.add(move.block)
            if not grown:
                break
            vocab |= grown
            frontier = grown
        _LOG.info(
            "route vocabulary: %d blocks from %d waypoint seeds",
            len(vocab), len(seeds),
        )
        return frozenset(vocab)

    def __contains__(self, block_a: str) -> bool:
        return block_a in self._moves

    def __len__(self) -> int:
        return len(self._moves)
