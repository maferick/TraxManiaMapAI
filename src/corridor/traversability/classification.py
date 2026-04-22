"""Family-level traversability classification.

**This is the root of truth for which block families participate in
corridor search.** Three disjoint buckets, hard membership, zero
heuristics. No scoring, no weighting, no context-dependence — those
live in downstream phases (edge labeling, path enumeration).

Contract: ``docs/workstreams/corridor-prereq-2-traversability.md``.

Sourcing philosophy
-------------------

Every family placement in this file is justified by:

1. Observation in the ``2026-04-scale-1k`` corpus audit (run
   ``python -m src.cli audit-block-families`` to regenerate current
   numbers; corpus-dependent so they drift).
2. Checkpoint-anchor family distribution — which families carry the
   ``WaypointSpecialProperty`` variants pulled into ``map_checkpoints``
   at parse time.
3. Top-pair adjacency volume in the Neo4j graph — which families
   dominate edge counts and therefore drive the deco-suppression goal.

Revision policy
---------------

This classification is **hand-authored** and expected to need review
when:

- a Nadeo client update adds a new block family
- the corpus grows enough that a rare family becomes common
- a map-author convention shifts (e.g. custom block usage patterns)

Changes here are in-repo decisions — bump the ``CLASSIFICATION_VERSION``
below and document the rationale in the commit. Audit output is NOT a
source of truth; it's input to this file.

Unknown families (not in any bucket) default to ``AMBIGUOUS`` so new
block types surface as review-required rather than silently joining a
bucket. Downstream code treats ``AMBIGUOUS`` as "needs explicit per-type
opt-in before entering the traversability subgraph."

The specifically-named ``Unknown`` family (user-imported custom
blocks) defaults to ``NON_DRIVABLE`` on purpose — map-author custom
imports are frequently decorative or out-of-bounds, and a fail-safe
default is the conservative choice. Specific custom blocks can be
whitelisted on a per-block-type basis in a later phase if needed.
"""
from __future__ import annotations

from enum import Enum
from typing import Final

# Bump when any family moves between buckets. Downstream artifacts
# (traversability_edge_evidence rows) should carry this version so
# reclassification is traceable. Semver-lite — major only at this stage.
CLASSIFICATION_VERSION: Final[str] = "0.1.0"


class FamilyBucket(str, Enum):
    """The three disjoint classification buckets.

    Intentionally a ``str`` Enum so the value can be persisted
    verbatim in database rows without custom serialization.
    """
    DRIVABLE = "drivable"
    NON_DRIVABLE = "non_drivable"
    AMBIGUOUS = "ambiguous"


# -----------------------------------------------------------------------------
# DRIVABLE — families whose blocks carry the car during a race.
# -----------------------------------------------------------------------------
# Rationale per family:
#
# Platform       Largest track-family presence (194k placements / 856 maps in
#                the scale-1k audit). Most modern TM2020 track maps are built
#                predominantly on Platform blocks. Includes the Checkpoint /
#                Start / Finish variants that show up as waypoint anchors.
#
# Road           Classic road family. Second-widest track-family presence by
#                map count (776 maps, ~43 placements/map average). All Road*
#                Checkpoint / Start / Finish variants live here.
#
# Track          "Track"-prefixed family showing up in top adjacency pairs
#                alongside Road and Platform (Road→Track, Platform→Track,
#                Track→Track). Frequently the hybrid-surface track pieces
#                connecting Road/Platform sections.
#
# Gate           Special case: gates are race-waypoint markers (checkpoint /
#                finish / start). The gate block itself is drivable *into* —
#                the car passes through the trigger plane — but the
#                downstream traversability subgraph should treat Gate edges
#                as waypoint crossings rather than interior path segments.
#                That distinction is per-edge labeling logic, not
#                family-level; at the classification layer, Gate is simply
#                DRIVABLE.
#
# Technics       "Tech"-specific variants (8.6k placements / 437 maps).
#                Present across many tech-style maps; drivable by design.
#
# Rally          Rally-style track family (44k placements but only 69 maps
#                — niche game mode). Drivable within its niche.
#
# Snow           Snow-surface track variants (5k placements / 94 maps).
#                Drivable; niche but real.
#
# Dirt           Dirt-surface track variants (3.4k placements / 5 maps).
#                Low corpus presence; classifier-level DRIVABLE so those
#                maps don't get empty corridor graphs. Revisit if dirt
#                usage grows meaningfully.
DRIVABLE_FAMILIES: Final[frozenset[str]] = frozenset({
    "Platform",
    "Road",
    "Track",
    "Gate",
    "Technics",
    "Rally",
    "Snow",
    "Dirt",
})


# -----------------------------------------------------------------------------
# NON_DRIVABLE — families that never participate in a race path.
# Suppressing these aggressively is the primary mechanism for hitting
# the §8.2 commit bar (≥80% of deco/support edges removed).
# -----------------------------------------------------------------------------
# Rationale per family:
#
# Deco           Single largest noise source. 2.24M placements dominated by
#                DecoWallBasePillar (1.4M) and DecoWallWaterBase (488k),
#                DecoPlatformBase variants (99k across surface types).
#                These are the under-track / beside-track bases that sit
#                physically adjacent to Platform/Road blocks but are not
#                drivable onto. Suppressing Deco is the single biggest
#                adjacency-graph pruning win.
#
# Structure      Track support pillars, stadium structural supports. 107k
#                placements / 552 maps. Sit directly under drivable blocks
#                but are not themselves drivable.
#
# Stand          Stadium stands (spectator seating). 12k placements / 200
#                maps. Obviously non-drivable.
#
# Canopy         Stadium roof / canopy panels. 9k placements / 185 maps.
#                Obviously non-drivable.
#
# Stadium        Stadium scenery props (grass, field markings, decorative
#                fixtures). 467k placements but concentrated in 62 maps
#                (mean 7.5k/map) — stadium-heavy maps carry huge volumes.
#                Non-drivable.
#
# Water          Water blocks — not drivable in TM2020. 91k placements /
#                272 maps.
#
# Void           Void / empty-space blocks. 81k placements / 4 maps (niche
#                but those maps use ~20k void blocks each).
#
# Lake           Lake / large-water environmental blocks.
#
# Ground         Ground / terrain blocks.
#
# Grass          Grass terrain.
#
# Land           Land / earth terrain.
#
# Unknown        User-imported custom blocks (block_type names like
#                "A-BlockGBX\TrenchGroundRemover.Block.Gbx_CustomBlock").
#                DEFAULT TO BLOCKED: map-author custom imports are
#                frequently decorative, out-of-bounds, or environmental
#                modifications (e.g. ground-removers). Whitelisting
#                specific drivable custom blocks belongs in a per-type
#                allowlist added in a later phase, not at the family
#                level.
NON_DRIVABLE_FAMILIES: Final[frozenset[str]] = frozenset({
    "Deco",
    "Structure",
    "Stand",
    "Canopy",
    "Stadium",
    "Water",
    "Void",
    "Lake",
    "Ground",
    "Grass",
    "Land",
    "Unknown",
})


# -----------------------------------------------------------------------------
# AMBIGUOUS — families that need per-block-type decisions, not a
# family-level call. Phase 3 evidence layer may promote specific
# block_types out of AMBIGUOUS via the traversability_edge_evidence
# artifact; at the family layer, these remain review-required.
# -----------------------------------------------------------------------------
# Rationale per family:
#
# Open           Shows up 99 times in map_checkpoints.block_name under
#                family "Open" — meaning SOME Open variants are drivable
#                checkpoint-capable blocks, but the 7k overall placements
#                are a mix. Needs per-type review.
#
# Stage          "Stage"-prefixed blocks could be drivable stage track
#                OR stadium stage-area props. 27k placements / 331 maps —
#                common enough that getting this wrong matters.
#
# Items          Custom item placements. Intent is map-author-specific;
#                3 appearances at checkpoint anchors suggests rare
#                drivability. Default AMBIGUOUS.
#
# Nations        Legacy TrackMania Nations collection name. 19k / 17 maps.
#                Rare in modern TM2020 but not impossible.
#
# Special        "Special"-prefixed blocks. 13k placements in 1 map —
#                effectively a map-specific family. AMBIGUOUS until
#                observed in more contexts.
#
# Trackmania     Generic collection prefix — heuristic misfire candidate
#                (the heuristic extracts the first CamelCase token, and
#                "Trackmania" prefixes generic fallback blocks).
#
# Tm             Another generic-prefix heuristic misfire candidate.
#
# Block          Generic family name — almost certainly heuristic misfire
#                from block_type names starting with "Block" or similar.
#
# Wood           Surface-material-sounding name that may be a heuristic
#                misfire (RoadWood* etc. should extract to Road).
#
# Plastic        Same concern as Wood.
AMBIGUOUS_FAMILIES: Final[frozenset[str]] = frozenset({
    "Open",
    "Stage",
    "Items",
    "Nations",
    "Special",
    "Trackmania",
    "Tm",
    "Block",
    "Wood",
    "Plastic",
})


# -----------------------------------------------------------------------------
# Disjoint-set invariant check — catches accidental reclassification
# mistakes at import time rather than silently preferring one bucket.
# -----------------------------------------------------------------------------
def _validate_buckets() -> None:
    pairs = (
        ("DRIVABLE", DRIVABLE_FAMILIES, "NON_DRIVABLE", NON_DRIVABLE_FAMILIES),
        ("DRIVABLE", DRIVABLE_FAMILIES, "AMBIGUOUS", AMBIGUOUS_FAMILIES),
        ("NON_DRIVABLE", NON_DRIVABLE_FAMILIES, "AMBIGUOUS", AMBIGUOUS_FAMILIES),
    )
    for a_name, a_set, b_name, b_set in pairs:
        overlap = a_set & b_set
        if overlap:
            raise RuntimeError(
                f"traversability classification buckets overlap: "
                f"{a_name} ∩ {b_name} = {sorted(overlap)}. "
                "A family must belong to exactly one bucket."
            )


_validate_buckets()


def classify_family(family: str) -> FamilyBucket:
    """Return the :class:`FamilyBucket` for a block family name.

    Unseen families default to :attr:`FamilyBucket.AMBIGUOUS` so new
    block types surface as review-required rather than silently
    adopting a default bucket. This applies to families that aren't
    yet in any of the three sets — NOT to the explicitly-classified
    ``Unknown`` family, which is deliberately ``NON_DRIVABLE`` (see
    module docstring).
    """
    if family in DRIVABLE_FAMILIES:
        return FamilyBucket.DRIVABLE
    if family in NON_DRIVABLE_FAMILIES:
        return FamilyBucket.NON_DRIVABLE
    if family in AMBIGUOUS_FAMILIES:
        return FamilyBucket.AMBIGUOUS
    return FamilyBucket.AMBIGUOUS
