"""Surface families — the walker's block vocabulary.

The clip walker was pinned to 13 hand-listed ``RoadTech`` blocks, so
every generated map was a tech road no matter what was asked for.
Nothing in the walker required that: it is clip-driven, and each
surface family is simply a different clip id over the same geometry.

There are two consumers with different needs, so there are two
resolvers.

``resolve`` serves :class:`ClipWalker`, which joins blocks by matching
route-clips. Under that rule two facts from the catalogue apply:
``RoadTechFC`` and ``RoadDirtFC`` are distinct clips so those pools
cannot interconnect, and every Platform* surface shares
``PlatFormFCSmall`` so those five can.

``resolve_pool`` serves :class:`GrammarWalker`, which joins blocks by
corpus evidence. Under that rule the clip constraints above simply do
not apply, and the corpus says so plainly: map 25192 runs
``PlatformTechStart`` (clip ``PlatformFCSmallRacing``) into
``PlatformWaterSpecialTurbo2`` (``PlatformWaterFCSmall``) into
``PlatformTechToDecoWall`` (``PlatFormFCSmall``), and map 4269 runs
platform into open road into road in three consecutive cells. So
``resolve_pool`` mixes freely, keeps clipless blocks, and can add the
universal ``Gate*`` arches that no surface family contains.

Every surface family ships its own Start, Finish, Multilap and 7-9
checkpoints, so any of them can form a complete route on its own.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from src.catalogue.loader import BlockDef

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Family:
    name: str
    prefix: str
    clip: str
    # None when the walker can build with this family; otherwise the
    # reason it cannot, surfaced to the caller instead of a dead end.
    unsupported: str | None = None


# Platform families are excluded from the CLIP walker only. Their
# gates (Start/Finish/Checkpoint/Multilap) expose only
# ``PlatformFCSmallRacing``, which no other platform block carries —
# verified across all 186 PlatformTech blocks, of which just the 4
# gates use it. Under clip matching there is no bridge, so the gates
# cannot be chained to the surface.
#
# That is a fact about clip matching, NOT about the game: corpus map
# 25192 is a real, published plastic map that places
# ``PlatformTechStart`` directly beside surface blocks whose clips do
# not match it. Platform tiles butt together and you drive over the
# seam. Use ``resolve_pool`` + :class:`GrammarWalker` to build these.
_PLATFORM_REASON = (
    "platform gates use an isolated clip (PlatformFCSmallRacing) with "
    "no bridge to surface blocks, so clip matching cannot chain them. "
    "Real platform maps do it anyway — build them with the grammar "
    "walker (resolve_pool) instead"
)

_OPEN_REASON = (
    "open roads ship checkpoints but no Start or Finish, so they "
    "cannot form a route alone; add them to another family's pool "
    "with resolve_pool"
)

FAMILIES: dict[str, Family] = {
    "tech": Family("tech", "RoadTech", "RoadTechFC"),
    "dirt": Family("dirt", "RoadDirt", "RoadDirtFC"),
    "bump": Family("bump", "RoadBump", "RoadBumpFC"),
    "ice": Family("ice", "RoadIce", "RoadIceFC"),
    "water": Family("water", "RoadWater", "RoadWaterVFC"),
    "platform-tech": Family(
        "platform-tech", "PlatformTech", "PlatFormFCSmall", _PLATFORM_REASON),
    "platform-dirt": Family(
        "platform-dirt", "PlatformDirt", "PlatFormFCSmall", _PLATFORM_REASON),
    "platform-ice": Family(
        "platform-ice", "PlatformIce", "PlatFormFCSmall", _PLATFORM_REASON),
    "platform-grass": Family(
        "platform-grass", "PlatformGrass", "PlatFormFCSmall", _PLATFORM_REASON),
    "platform-plastic": Family(
        "platform-plastic", "PlatformPlastic", "PlatFormFCSmall", _PLATFORM_REASON),
    # Open roads have checkpoints but no Start/Finish of their own, so
    # they are a garnish on another family rather than a whole map.
    # Map 4269 uses OpenTechRoadStraight mid-route between a platform
    # block and a road block.
    "open-tech": Family("open-tech", "OpenTechRoad", "", _OPEN_REASON),
    "open-dirt": Family("open-dirt", "OpenDirtRoad", "", _OPEN_REASON),
}

# Universal gate arches. Clipless and 1x4x1 (GateCheckpoint,
# GateFinish, GateSpecialTurbo, ...) or 1x1x1 tiles a mapper assembles
# into a wall (GateExpandable*). They belong to no surface family,
# which is why a prefix-based pool never saw them, which is why the
# clip walker could not close a plastic map.
GATE_PREFIX = "Gate"

SUPPORTED = [n for n, f in FAMILIES.items() if f.unsupported is None]

# Every family is usable by the grammar walker: it does not chain by
# clips, so nothing above disqualifies a pool.
GRAMMAR_FAMILIES = sorted(FAMILIES)


class FamilyError(ValueError):
    pass


def resolve_pool(
    names: list[str],
    catalogue: dict[str, BlockDef],
    max_footprint: int | None = None,
    gates: bool = True,
) -> list[str]:
    """Block ids for the grammar walker: no clip requirement, no mixing rule.

    Three things ``resolve`` drops and this keeps, each because the
    corpus contains it:

    * **clipless blocks.** Every 1x4x1 ``Gate*`` arch has no clips at
      all. ``resolve`` filters on exposing a route clip, so those
      blocks were invisible to it — and they are how a map gets a
      checkpoint or a booster over a surface that has no gate of its
      own.
    * **cross-family pools.** Mixing tech with dirt is rejected under
      clip matching. Real maps do it.
    * **families marked unsupported.** That flag describes the clip
      walker's reach, not the game's.

    ``gates`` adds the universal ``Gate*`` set on top of the requested
    families. Leave it on unless a run deliberately wants no arches.
    """
    if not names:
        raise FamilyError("no families requested")
    unknown = [n for n in names if n not in FAMILIES]
    if unknown:
        raise FamilyError(
            f"unknown families {unknown}; available: {sorted(FAMILIES)}"
        )

    prefixes = tuple(FAMILIES[n].prefix for n in names)
    if gates:
        prefixes += (GATE_PREFIX,)

    allowed: list[str] = []
    for block_id, block in catalogue.items():
        if not block_id.startswith(prefixes):
            continue
        variant = block.variant("ground", 0)
        if variant is None:
            continue
        if max_footprint is not None:
            if variant.size[0] > max_footprint or variant.size[2] > max_footprint:
                continue
        allowed.append(block_id)

    have_start = any(catalogue[b].waypoint == "Start" for b in allowed)
    have_finish = any(catalogue[b].waypoint == "Finish" for b in allowed)
    if not (have_start and have_finish):
        raise FamilyError(
            f"families {names} lack a Start and/or Finish block"
        )

    _LOG.info(
        "families %s -> %d blocks (gates=%s)", names, len(allowed), gates
    )
    return sorted(allowed)


def resolve(
    names: list[str],
    catalogue: dict[str, BlockDef],
    max_footprint: int | None = None,
) -> tuple[list[str], frozenset[str]]:
    """Block ids + route clips for the requested families.

    Returns what :class:`ClipWalker` needs. Mixing families is only
    allowed when they share a clip (the Platform* set); mixing
    ``tech`` with ``dirt`` would produce a block pool whose halves
    can never connect, so it is rejected up front rather than
    failing later as a mysterious dead end.

    ``max_footprint`` caps block size in XZ, useful for keeping a
    route inside a small map.
    """
    if not names:
        raise FamilyError("no families requested")
    unknown = [n for n in names if n not in FAMILIES]
    if unknown:
        raise FamilyError(
            f"unknown families {unknown}; available: {sorted(FAMILIES)}"
        )

    chosen = [FAMILIES[n] for n in names]
    blocked = [f for f in chosen if f.unsupported]
    if blocked:
        raise FamilyError(
            f"{blocked[0].name}: {blocked[0].unsupported}. "
            f"Supported families: {SUPPORTED}"
        )
    clips = {f.clip for f in chosen}
    if len(clips) > 1:
        raise FamilyError(
            f"families {names} use different clips {sorted(clips)} and "
            "cannot interconnect; generate them separately or add "
            "transition blocks"
        )

    prefixes = tuple(f.prefix for f in chosen)
    allowed: list[str] = []
    for block_id, block in catalogue.items():
        if not block_id.startswith(prefixes):
            continue
        variant = block.variant("ground", 0)
        if variant is None:
            continue
        if max_footprint is not None:
            if variant.size[0] > max_footprint or variant.size[2] > max_footprint:
                continue
        # Must expose at least one port on this family's clip,
        # otherwise the walker can never enter or leave it.
        if not any(
            p.clip_id in clips for p in variant.side_ports()
        ):
            continue
        allowed.append(block_id)

    have_start = any(catalogue[b].waypoint == "Start" for b in allowed)
    have_finish = any(catalogue[b].waypoint == "Finish" for b in allowed)
    if not (have_start and have_finish):
        raise FamilyError(
            f"families {names} lack a Start and/or Finish block"
        )

    _LOG.info(
        "families %s -> %d blocks, clips %s",
        names, len(allowed), sorted(clips),
    )
    return sorted(allowed), frozenset(clips)
