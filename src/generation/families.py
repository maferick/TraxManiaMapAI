"""Surface families — the walker's block vocabulary.

The clip walker was pinned to 13 hand-listed ``RoadTech`` blocks, so
every generated map was a tech road no matter what was asked for.
Nothing in the walker required that: it is clip-driven, and each
surface family is simply a different clip id over the same geometry.

Two facts from the catalogue shape this module:

* **Roads do not interconnect across surfaces.** ``RoadTechFC`` and
  ``RoadDirtFC`` are distinct clips, so a tech road cannot butt
  straight into a dirt road — real maps use explicit transition
  blocks for that, which is a separate feature.
* **Platforms all share ``PlatFormFCSmall``.** Every Platform*
  surface (tech / dirt / ice / grass / plastic) uses the same clip,
  so those five families interconnect freely and can be mixed in one
  route.

Every family ships exactly one Start and one Finish plus 7-9
checkpoints, so any of them can form a complete route.
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


# Platform families are deliberately excluded. Their gates
# (Start/Finish/Checkpoint/Multilap) expose only
# ``PlatformFCSmallRacing``, which NO other platform block carries —
# verified across all 186 PlatformTech blocks, of which just the 4
# gates use it. There is no bridge block, so gates cannot be chained
# to platform surface blocks at all. Platform maps are not linear
# clip-chains: the gates sit on a platform area rather than in-line.
# Supporting them needs a different route model, not a config entry.
_PLATFORM_REASON = (
    "platform gates use an isolated clip (PlatformFCSmallRacing) with "
    "no bridge to surface blocks; platform maps are not linear "
    "clip-chains and need a different route model"
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
}

SUPPORTED = [n for n, f in FAMILIES.items() if f.unsupported is None]


class FamilyError(ValueError):
    pass


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
