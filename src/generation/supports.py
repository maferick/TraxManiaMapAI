"""Support pillars under elevated route blocks.

Generated routes place only the driving line, so elevated sections
hang in mid-air — the most obvious "not built by a person" tell.
The game itself fills those columns automatically, but ONLY when a
human places a block in the editor; a map written straight to
``.Map.Gbx`` gets none, and re-saving does not backfill them.

Since the offline writer is the production path (headless, batchable
— see ``tools/harvest_pillar_rules.py`` for why that matters), the
emitter has to reproduce the game's behaviour itself.

**The rules are a lookup table, not a formula.** Three successive
guesses failed against the game:

1. per-cell 1x1 fill -> tiled into a solid concrete plateau
2. footprint-matched pillar at the road's rotation -> wrong variants
3. name rewrite ``RoadTech<X>`` -> ``TrackWall<X>Pillar`` with a
   foot/transition/shaft variant stack -> looked right on the
   handful of RoadTech blocks it was derived from

A full harvest against the game (3,318 blocks) showed why #3 could
never generalise: the name rewrite predicts the correct pillar for
just 197 of 2,918 blocks (7%). Pillar direction varies across all
four values, 73 blocks place their pillar off the block anchor, and
cliff blocks map to pillar names sharing no stem at all.

So this module reads ``data/catalogue/pillar_rules.json``, produced
by asking the game directly. Blocks absent from the table get no
pillars rather than a guessed one.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from src.catalogue.loader import BlockDef, rotate_offset
from src.generation.clip_walker import GROUND_Y, Placement

_LOG = logging.getLogger(__name__)

SUPPORTS_VERSION = "supports-v4"
SCHEMA = "pillar_rules_v3"
DEFAULT_RULES_PATH = "data/catalogue/pillar_rules.json"


@dataclass(frozen=True)
class PillarSlot:
    """One pillar within a level's pattern, offset from the anchor."""

    dx: int
    dz: int
    pillar: str
    variant: int
    direction: int


@dataclass(frozen=True)
class PillarRule:
    # A level's full pattern. Multi-cell blocks get one slot per
    # footprint cell and the slots can differ - a RoadTechToRoadBump
    # transition puts dir=2 under its tech end and dir=0 under its
    # bump end.
    pattern: tuple[PillarSlot, ...]
    uniform: bool


class PillarRules:
    """Game-harvested pillar table, keyed by road block id."""

    def __init__(self, rules: dict[str, PillarRule], ground_y: int) -> None:
        self._rules = rules
        self.ground_y = ground_y

    @classmethod
    def load(cls, path: str | Path = DEFAULT_RULES_PATH) -> "PillarRules":
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        if doc.get("schema") != SCHEMA:
            raise ValueError(f"unsupported pillar-rule schema: {doc.get('schema')!r}")
        rules = {
            block_id: PillarRule(
                pattern=tuple(
                    PillarSlot(
                        dx=int(s["dx"]), dz=int(s["dz"]),
                        pillar=str(s["pillar"]), variant=int(s["variant"]),
                        direction=int(s["dir"]),
                    )
                    for s in r.get("pattern", [])
                ),
                uniform=bool(r.get("uniform", True)),
            )
            for block_id, r in doc.get("rules", {}).items()
        }
        _LOG.info("pillar rules loaded: %d blocks from %s", len(rules), path)
        return cls(rules, int(doc.get("ground_y", GROUND_Y)))

    def get(self, block_id: str) -> PillarRule | None:
        return self._rules.get(block_id)

    def __len__(self) -> int:
        return len(self._rules)


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
    rules: PillarRules,
    ground_y: int | None = None,
) -> list[Placement]:
    """Pillars beneath elevated route blocks, per the harvested table.

    One pillar per level from ground up to the block. A level is
    skipped when its footprint would hit anything already placed —
    the route, or a pillar from an earlier block — which is what
    keeps self-crossing routes safe.
    """
    ground = rules.ground_y if ground_y is None else ground_y
    cells = route_cells(placements, catalogue)
    occupied = set(cells)
    supports: list[Placement] = []
    unknown: set[str] = set()
    non_uniform: set[str] = set()

    # Bottom-up. Where a route stacks in one column, the game supports
    # the LOWEST block and the upper deck inherits that column, so the
    # lowest block must claim it first. Processing in route order let
    # an upper 1x1 curve grab a column the lower 2x2 curve owned,
    # which the game-diff caught as 24 wrong cells.
    for p in sorted(placements, key=lambda q: (q.y, q.z, q.x)):
        own = _footprint(p.block_id, p.x, p.y, p.z, p.rotation, catalogue)
        if not own:
            continue
        base_y = min(c[1] for c in own)
        if base_y <= ground:
            continue

        rule = rules.get(p.block_id)
        if rule is None:
            unknown.add(p.block_id)
            continue
        if not rule.uniform:
            # The harvest recorded only the bottom level for these;
            # applying it to the whole column would be a guess.
            non_uniform.add(p.block_id)
            continue

        # The table was harvested with the probe at rotation 0, so a
        # slot must be transported into the placed block's frame.
        #
        # Rotating the slot OFFSET directly is wrong: rotate_offset
        # re-anchors so a footprint's min corner sits at the origin,
        # which is right for unit cells but moves a multi-cell
        # pillar's anchor (a 2x2 pillar under a 2x2 block at rotation
        # 1 came out at (1,0) instead of (0,0), which the game-diff
        # caught as 15 wrong cells on a dirt route).
        #
        # So rotate the CELLS the slot covers and take their min
        # corner as the new anchor. That is correct for both a 1x1
        # pillar per cell and a pillar spanning the whole footprint.
        road = catalogue.get(p.block_id)
        road_variant = road.variant("ground", 0) if road else None
        road_size = road_variant.size if road_variant else (1, 1, 1)

        for y in range(ground, base_y):
            for slot in rule.pattern:
                srot = (slot.direction + p.rotation) % 4
                local = _footprint(
                    slot.pillar, slot.dx, 0, slot.dz, slot.direction, catalogue
                )
                if not local:
                    continue
                moved = [
                    rotate_offset((cx, 0, cz), p.rotation, road_size)
                    for cx, _cy, cz in local
                ]
                ax = p.x + min(c[0] for c in moved)
                az = p.z + min(c[2] for c in moved)
                want = _footprint(slot.pillar, ax, y, az, srot, catalogue)
                if not want or any(c in occupied for c in want):
                    continue
                supports.append(Placement(
                    slot.pillar, ax, y, az, srot, variant=slot.variant,
                ))
                occupied.update(want)

    if unknown:
        _LOG.warning(
            "%s: no harvested rule for %s — left unsupported rather "
            "than guessed", SUPPORTS_VERSION, sorted(unknown)[:6],
        )
    if non_uniform:
        _LOG.warning(
            "%s: non-uniform pillar column for %s — needs per-level "
            "harvest", SUPPORTS_VERSION, sorted(non_uniform)[:6],
        )
    _LOG.info(
        "%s: %d pillars for %d route blocks",
        SUPPORTS_VERSION, len(supports), len(placements),
    )
    return supports
