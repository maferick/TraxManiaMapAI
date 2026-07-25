"""Load the offline block catalogue and answer rotation questions.

The catalogue (``block_catalogue_v1`` NDJSON, produced by the
BlockCatalogueDump plugin + the wrapper's ``dump-block-catalogue``
verb) is the game's own block model: per-variant unit cells and
per-unit, per-face clip lists. Two blocks join when their facing
clips carry the same clip id — this module exposes exactly that
relation, plus the rotation math needed to ask it for a block
placed at any of the four grid directions.

Face convention — EMPIRICAL, calibrated in-game 2026-07-25 with the
RotationCalibration map (isolated Curve1 at rotations 0..3; the
game's yellow dead-end caps mark open faces):

    rotation:              0        1        2        3
    Curve1 (local n,e):  -x,+z    -x,-z    +x,-z    +x,+z

Which pins the model: a local face ``f`` at rotation ``d`` looks at
world face ``(f + d) % 4``, where the world deltas are

    face 0 = "north" = +z
    face 1 = "east"  = -x
    face 2 = "south" = -z
    face 3 = "west"  = +x

(The face NAMES follow the catalogue's clip keys; their world
directions are the game's local-frame convention as measured — do
not "correct" them to intuition. Two earlier wrong guesses shipped:
``+d`` with mirrored deltas broke every curve, ``-d`` broke curves
at even rotations only.) Multi-cell unit offsets rotate one ring
step per ``d`` in the same sense, re-anchored so offsets stay
non-negative (the placement coord stays the min corner).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

_LOG = logging.getLogger(__name__)

SCHEMA = "block_catalogue_v1"

FACE_NORTH = 0
FACE_EAST = 1
FACE_SOUTH = 2
FACE_WEST = 3
SIDE_FACES = ("n", "e", "s", "w")

# World-space cell delta for each side face. EMPIRICAL — see module
# docstring; calibrated against the game, not a naming intuition.
FACE_DELTAS: dict[int, tuple[int, int, int]] = {
    FACE_NORTH: (0, 0, 1),
    FACE_EAST: (-1, 0, 0),
    FACE_SOUTH: (0, 0, -1),
    FACE_WEST: (1, 0, 0),
}


def opposite_face(face: int) -> int:
    return (face + 2) % 4


def rotate_face(face: int, direction: int) -> int:
    """World face a local side face looks at after rotating by ``direction``."""
    return (face + direction) % 4


def rotate_offset(
    offset: tuple[int, int, int],
    direction: int,
    size: tuple[int, int, int],
) -> tuple[int, int, int]:
    """Rotate a unit offset clockwise ``direction`` quarter-turns.

    ``size`` is the unrotated variant footprint. The result is
    re-anchored so the rotated footprint's min corner is (0, 0, 0).
    """
    x, y, z = offset
    sx, _, sz = size
    d = direction % 4
    if d == 0:
        return (x, y, z)
    if d == 1:
        # One ring step in the calibrated face sense (n->e means the
        # +z-looking face turns to look at -x): (vx, vz) -> (-vz, vx).
        return (sz - 1 - z, y, x)
    if d == 2:
        return (sx - 1 - x, y, sz - 1 - z)
    return (z, y, sx - 1 - x)


def rotated_size(size: tuple[int, int, int], direction: int) -> tuple[int, int, int]:
    sx, sy, sz = size
    return (sz, sy, sx) if direction % 2 == 1 else (sx, sy, sz)


@dataclass(frozen=True)
class UnitPort:
    """One drivable/connectable face of one unit cell, in local space."""

    offset: tuple[int, int, int]
    face: int  # SIDE face index at rotation 0
    clip_id: str


@dataclass(frozen=True)
class BlockUnit:
    offset: tuple[int, int, int]
    underground: bool
    terrain_modifier: str
    surface: str
    clips: dict[str, tuple[str, ...]]  # keys: n e s w top bottom


@dataclass(frozen=True)
class BlockVariant:
    kind: str  # "ground" | "air"
    index: int
    size: tuple[int, int, int]
    units: tuple[BlockUnit, ...]

    def side_ports(self) -> tuple[UnitPort, ...]:
        """All side-face clip ports of this variant, local space."""
        ports: list[UnitPort] = []
        for unit in self.units:
            for face_idx, face_key in enumerate(SIDE_FACES):
                for clip_id in unit.clips.get(face_key, ()):
                    ports.append(UnitPort(unit.offset, face_idx, clip_id))
        return tuple(ports)


@dataclass(frozen=True)
class BlockDef:
    block_id: str
    name: str
    page: str
    waypoint: str  # "Start" | "Finish" | "Checkpoint" | "None" | ...
    is_pillar: bool
    variants: tuple[BlockVariant, ...]

    def variant(self, kind: str = "ground", index: int = 0) -> BlockVariant | None:
        for v in self.variants:
            if v.kind == kind and v.index == index:
                return v
        return None


def load_catalogue(path: str | Path) -> dict[str, BlockDef]:
    """Parse a catalogue NDJSON into {block_id: BlockDef}.

    Refuses a catalogue without its ``catalogue.done.json`` completion
    marker — a partial dump must never silently become the substrate.
    """
    path = Path(path)
    done_path = path.with_name("catalogue.done.json")
    if not done_path.is_file():
        raise FileNotFoundError(
            f"catalogue completion marker missing: {done_path}"
        )

    blocks: dict[str, BlockDef] = {}
    meta_seen = False
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rec_type = rec.get("type")
            if rec_type == "meta":
                if rec.get("schema") != SCHEMA:
                    raise ValueError(
                        f"unsupported catalogue schema: {rec.get('schema')!r}"
                    )
                meta_seen = True
                continue
            if rec_type != "block":
                continue
            block = _parse_block(rec)
            blocks[block.block_id] = block

    if not meta_seen:
        raise ValueError(f"no meta record in catalogue: {path}")
    _LOG.info("catalogue loaded: %d blocks from %s", len(blocks), path)
    return blocks


def _parse_block(rec: dict) -> BlockDef:
    variants: list[BlockVariant] = []
    for v in rec.get("variants", []):
        units = tuple(
            BlockUnit(
                offset=tuple(u.get("offset", (0, 0, 0))),
                underground=bool(u.get("underground", False)),
                terrain_modifier=str(u.get("terrain_modifier", "")),
                surface=str(u.get("surface", "")),
                clips={
                    key: tuple(vals)
                    for key, vals in u.get("clips", {}).items()
                },
            )
            for u in v.get("units", [])
        )
        variants.append(
            BlockVariant(
                kind=str(v.get("kind", "ground")),
                index=int(v.get("index", 0)),
                size=tuple(v.get("size", (1, 1, 1))),
                units=units,
            )
        )
    # Runtime dumps carry ints, offline dumps carry enum names.
    waypoint = rec.get("waypoint", "None")
    if isinstance(waypoint, int):
        waypoint = {
            0: "Start", 1: "Finish", 2: "Checkpoint",
            3: "None", 4: "StartFinish", 5: "Dispenser",
        }.get(waypoint, "None")
    return BlockDef(
        block_id=str(rec["id"]),
        name=str(rec.get("name", "")),
        page=str(rec.get("page", "")),
        waypoint=str(waypoint),
        is_pillar=bool(rec.get("is_pillar", False)),
        variants=tuple(variants),
    )
