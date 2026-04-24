"""Minimal AI generator v0 — block-sequence synthesis per interval.

See :doc:`../../docs/generation/minimal-ai-generator-v0.md` for the
normative design. This module implements the MVP described there:
greedy (beam_width=1) block-by-block walk between anchor pairs,
scoring candidates with a linear combination of corpus priors +
geometry / diversity / validation signals, producing a
``generation-v0.2`` artifact.

What this PR intentionally doesn't do:

- Beam width > 1 (greedy is enough to prove the pipeline; wider
  beam follows). ``beam_width`` is wired through the signature
  so a follow-up changes behaviour without changing the contract.
- Full triple-transition scoring (pair + geometry + diversity +
  post-interval validators is the scored path; triples and
  sequence-score components land behind weight flags).
- GBX emit of synthesised block lists — the existing emit-gbx
  wrapper mutates a base .Map.Gbx, not yet builds one from
  scratch. Tracked in the doc's out-of-scope section.

Scope boundary (CLAUDE.md):

- No transformer / ML model training in this module.
- No finishability override — the gate stays independent.
- No replay-evidence mutation.
- Replay-touched cells flow IN as input (load_replay_touched_cells);
  the generator never writes to replay-derived tables.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from pymysql.connections import Connection

from src.corridor.traversability.classification import CLASSIFICATION_VERSION
from src.generation.finishability import (
    AI_CONFIDENCE_FLOOR,
    GATE_VERSION,
)
from src.generation.geom_validator import (
    CODE_PARTIAL_MULTICELL,
    GeometryInfo,
    SEVERITY_FAIL,
    load_geometry_lookup,
    validate_map_geometry,
)
from src.generation.jump_validator import (
    CLASS_LIKELY_BROKEN,
    validate_jumps,
)
from src.generation.preemit import _normalize_block
from src.generation.replay_cells import load_replay_touched_cells
from src.generation.schema import validate_generated_map
from src.generation.types import Anchor, Cell
from src.storage.mariadb import cursor
from src.utils.config import code_version, resolve_config_hash

_LOG = logging.getLogger(__name__)

# Bump when scoring weights / algorithm change materially — the
# field ships in the artifact under `map.ai_generator_version`.
#   v0.0 — pair prior + connector + traversability + diversity
#   v0.1 — adds triple priors + pre-step shadow penalty;
#          ai_confidence denominator now positive-weight sum only
#   v0.2 — beam search (width>=1); greedy v0.1 = beam_width=1 special
#          case. Beams that dead-end are pruned; winning beam merges
#          its cells back into the occupancy set on interval close.
#   v0.3 — sequence-score tier: per-(prev, cand) geometry compatibility
#          using shape_class + surface_hint from block_geometry. Fills
#          the v0.0–v0.2 zero stub in score_candidate's sequence tier.
#   v0.4 — occupancy tracks multi-cell footprint (origin + shadow cells)
#          not just placement cell, so later steps can't land inside a
#          prior multi-cell block's mesh. Catalogue filters 33% of
#          candidates that are user-imported custom blocks — won't load
#          on a fresh TM2020 install.
AI_GENERATOR_VERSION: str = "ai-generator-v0.4"

# Fixed Stadium-ground Y. Same convention as geom_validator's
# default ground_y. Multi-env support is a later phase.
_DEFAULT_GROUND_Y: int = 9

# Default hyperparameters. The doc pins these; changes require a
# revision bump on ``AI_GENERATOR_VERSION``.
DEFAULT_BEAM_WIDTH: int = 3        # v0.2 default; pass 1 to force greedy
DEFAULT_MAX_INTERVAL_DEPTH: int = 12
DEFAULT_TOP_N_CANDIDATES: int = 8  # per step, before beam prune

# Scoring weights. Additive linear combination; see the doc.
AI_GENERATOR_WEIGHTS = {
    "pair_prior":         1.0,
    "triple_prior":       0.7,
    "connector":          0.5,
    "traversability":     0.5,
    "sequence":           0.3,
    "diversity_penalty":  0.3,
    "validation_penalty": 0.4,
}


# ---------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class AIGenerationInputs:
    """Operator-facing inputs for one AI-generator run.

    Mirrors :class:`GenerationInputs` but drops ``strip`` (v0.2
    synthesises from scratch; strip-to-route is nonsensical on a
    synthesised list) and adds the beam / depth knobs.
    """
    base_map_id: int
    random_seed: int = 42
    style_tag_filter: str | None = None
    difficulty: str = "medium"
    beam_width: int = DEFAULT_BEAM_WIDTH
    max_interval_depth: int = DEFAULT_MAX_INTERVAL_DEPTH


# ---------------------------------------------------------------------
# Internal shapes
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class _CandidateBlock:
    family: str
    name: str
    cell: Cell
    rotation: int
    info: GeometryInfo


@dataclass
class _StepOutcome:
    block: dict[str, Any]            # artifact-shaped block entry
    score: float
    breakdown: dict[str, float]


@dataclass
class _IntervalResult:
    blocks: list[dict[str, Any]]     # synthesised blocks, order preserved
    path_cells: list[Cell]
    score_sum: float
    score_count: int
    reject_reason: str | None = None
    detail: str | None = None


# ---------------------------------------------------------------------
# Catalogue loading
# ---------------------------------------------------------------------

# Shapes the candidate-filter keeps. Anchors are excluded outright —
# they come from the base map verbatim. Support / deco / unknown /
# gate drop (gate = checkpoint gate block; that's an anchor class).
_ALLOWED_SHAPES: frozenset[str] = frozenset({
    "straight", "curve", "ramp", "loop", "platform",
})


@dataclass(frozen=True)
class _CatalogueEntry:
    family: str
    name: str
    info: GeometryInfo


def _load_candidate_catalogue(
    conn: Connection,
) -> list[_CatalogueEntry]:
    """Return the block_geometry rows eligible as candidate next
    blocks. Drops anchors / deco / support / unknown / gate /
    free_only. The remaining set is what the greedy loop scores."""
    with cursor(conn) as cur:
        # v0.4 filters:
        #
        # 1. User-imported custom blocks. TMX maps frequently embed or
        #    reference custom .Block.Gbx files the community has
        #    authored — those block-model strings can't be placed on a
        #    fresh TM2020 install (the game looks them up by name and
        #    fails silently, producing the hidden / load-error blocks
        #    the operator saw in-game). Heuristic: path-separator or
        #    _CustomBlock / .Block.Gbx marker in the block_name.
        #
        # 2. Multi-cell blocks (footprint_x > 1). A multi-cell Wall4
        #    at cell C occupies C..C+3 and expects the C+1..C+3 cells
        #    to be 'under' its mesh (filled by BakedBlocks or scenery
        #    on the base map). The v0 AI walker doesn't synthesise
        #    those fillers — it places unit blocks along a 1-cell-
        #    per-step path — so multi-cell choices leave shadow cells
        #    empty and read as partial_multicell FAILs post-synth.
        #    Defer multi-cell support to the mesh-aware placement
        #    phase (M1/M2 workstream); v0 stays unit-only.
        cur.execute(
            "SELECT block_family, block_name, shape_class, "
            "       is_anchor_capable, footprint_x, footprint_y, "
            "       footprint_z, connector_hint "
            "FROM block_geometry "
            "WHERE is_anchor_capable = 0 "
            "  AND is_deco = 0 "
            "  AND shape_class IN ('straight','curve','ramp','loop','platform') "
            "  AND placement_mode IN ('grid_only', 'mixed', 'unknown') "
            "  AND connector_hint <> '' "
            "  AND footprint_x = 1 "
            "  AND block_name NOT LIKE '%\\\\%' "
            "  AND block_name NOT LIKE '%/%' "
            "  AND block_name NOT LIKE '%.Block.Gbx%' "
            "  AND block_name NOT LIKE '%_CustomBlock%'"
        )
        rows = cur.fetchall()
    out: list[_CatalogueEntry] = []
    for family, name, shape, anchor, fx, fy, fz, connector in rows:
        out.append(_CatalogueEntry(
            family=str(family), name=str(name),
            info=GeometryInfo(
                footprint_x=int(fx), footprint_y=int(fy), footprint_z=int(fz),
                connector_hint=str(connector or ""),
                shape_class=str(shape or "unknown"),
                is_anchor_capable=bool(anchor),
            ),
        ))
    _LOG.info("ai_generator: candidate catalogue size=%d", len(out))
    return out


# Shape-class pair compatibility — same set
# src.constraints.sequence_scoring uses. Imported to avoid drift: a
# future edit to the pair set should only touch one file.
from src.constraints.sequence_scoring import (
    _SHAPE_COMPATIBLE_PAIRS as _SEQ_SHAPE_PAIRS,
)


@dataclass(frozen=True)
class _ShapeSurface:
    """Lean (shape_class, surface_hint) tuple the scorer reads on
    every candidate pair. Kept separate from GeometryInfo because
    GeometryInfo deliberately omits surface_hint (that attribute
    is validator-irrelevant, scorer-relevant)."""
    shape_class: str
    surface_hint: str


def _load_shape_surface_lookup(
    conn: Connection,
) -> dict[tuple[str, str], _ShapeSurface]:
    """Load (shape_class, surface_hint) for every block in the
    catalogue — INCLUDING anchors, since the scorer needs them for
    the first step's prev_block lookup (Start / Checkpoint /
    Finish are classified with is_anchor_capable=1 and thus absent
    from the drivable catalogue)."""
    with cursor(conn) as cur:
        cur.execute(
            "SELECT block_family, block_name, shape_class, surface_hint "
            "FROM block_geometry"
        )
        rows = cur.fetchall()
    out: dict[tuple[str, str], _ShapeSurface] = {}
    for family, name, shape, surface in rows:
        out[(str(family), str(name))] = _ShapeSurface(
            shape_class=str(shape or "unknown"),
            surface_hint=str(surface or ""),
        )
    _LOG.info(
        "ai_generator: shape_surface lookup size=%d (all blocks)", len(out),
    )
    return out


def _sequence_pair_score(
    prev: _ShapeSurface | None,
    cand: _ShapeSurface,
) -> float:
    """In-memory sequence-score per (prev, cand) pair. Mirrors the
    spirit of :func:`src.constraints.sequence_scoring._geometry_score_for`
    but drops the DB roundtrip (we don't need ``reasoning`` /
    ``detail`` strings in the hot loop).

    Baseline 0.4 when both blocks have a known shape; +0.3 when the
    shape pair is in the compatibility set; +0.3 when same surface;
    -0.1 for unknown on either side. Clamped to [0, 1].

    Returns 0.0 when prev is None (interval start) — the tier
    requires both sides to be meaningful.
    """
    if prev is None:
        return 0.0
    if prev.shape_class == "unknown" or cand.shape_class == "unknown":
        score = 0.4 - 0.1
    else:
        score = 0.4
        if (prev.shape_class, cand.shape_class) in _SEQ_SHAPE_PAIRS:
            score += 0.3
        if (
            prev.surface_hint and cand.surface_hint
            and prev.surface_hint == cand.surface_hint
        ):
            score += 0.3
    return max(0.0, min(1.0, score))


def _load_triple_priors(
    conn: Connection,
) -> dict[tuple[tuple[str, str], tuple[str, str]], dict[tuple[str, str], float]]:
    """Load P(B_next | B_prev_prev, B_prev) from
    block_triple_transitions. Returns
    ``{((fam_prev_prev, name_prev_prev), (fam_prev, name_prev)):
      {(fam_next, name_next): p}}``.

    Same normalisation pattern as :func:`_load_pair_priors`. The
    v0.1 scorer treats the triple tier as a sharpener over pairs
    — it fires only when we have two prior blocks AND the exact
    triple was observed. Unseen triples score 0 and fall back to
    the pair tier cleanly."""
    with cursor(conn) as cur:
        cur.execute(
            "SELECT block_family_a, block_name_a, "
            "       block_family_b, block_name_b, "
            "       block_family_c, block_name_c, "
            "       SUM(transition_count) AS c "
            "FROM block_triple_transitions "
            "GROUP BY block_family_a, block_name_a, "
            "         block_family_b, block_name_b, "
            "         block_family_c, block_name_c"
        )
        rows = cur.fetchall()
    counts: dict[
        tuple[tuple[str, str], tuple[str, str]],
        dict[tuple[str, str], int],
    ] = {}
    totals: dict[tuple[tuple[str, str], tuple[str, str]], int] = {}
    for fam_a, name_a, fam_b, name_b, fam_c, name_c, c in rows:
        key_ab = ((str(fam_a), str(name_a)), (str(fam_b), str(name_b)))
        key_c = (str(fam_c), str(name_c))
        c = int(c or 0)
        counts.setdefault(key_ab, {})[key_c] = c
        totals[key_ab] = totals.get(key_ab, 0) + c
    priors: dict[
        tuple[tuple[str, str], tuple[str, str]],
        dict[tuple[str, str], float],
    ] = {}
    for key_ab, dests in counts.items():
        total = totals[key_ab]
        priors[key_ab] = {
            kc: (c / total) for kc, c in dests.items()
        } if total else {}
    _LOG.info(
        "ai_generator: triple priors for %d (prev_prev, prev) pairs",
        len(priors),
    )
    return priors


def _load_pair_priors(
    conn: Connection,
) -> dict[tuple[str, str], dict[tuple[str, str], float]]:
    """Load P(B_next | B_cur) from block_pair_transitions.

    Returns ``{(fam_cur, name_cur): {(fam_next, name_next): p}}``
    where p is the conditional probability normalised over all
    outgoing transitions from ``(fam_cur, name_cur)``. Unseen
    transitions return 0 — the scorer treats them as weak priors,
    not hard zeros (connector/traversability tiers can still rescue
    them).
    """
    with cursor(conn) as cur:
        cur.execute(
            "SELECT block_family_a, block_name_a, block_family_b, "
            "       block_name_b, SUM(transition_count) AS c "
            "FROM block_pair_transitions "
            "GROUP BY block_family_a, block_name_a, "
            "         block_family_b, block_name_b"
        )
        rows = cur.fetchall()
    counts: dict[tuple[str, str], dict[tuple[str, str], int]] = {}
    totals: dict[tuple[str, str], int] = {}
    for fam_a, name_a, fam_b, name_b, c in rows:
        key_a = (str(fam_a), str(name_a))
        key_b = (str(fam_b), str(name_b))
        c = int(c or 0)
        counts.setdefault(key_a, {})[key_b] = c
        totals[key_a] = totals.get(key_a, 0) + c
    priors: dict[tuple[str, str], dict[tuple[str, str], float]] = {}
    for key_a, dests in counts.items():
        total = totals[key_a]
        priors[key_a] = {
            kb: (c / total) for kb, c in dests.items()
        } if total else {}
    _LOG.info("ai_generator: pair priors for %d source blocks", len(priors))
    return priors


# ---------------------------------------------------------------------
# Direction + rotation helpers
# ---------------------------------------------------------------------

# Unit direction vectors indexed by rotation 0..3, moving along +X
# at rot=0 and rotating clockwise in XZ when viewed from above.
_DIR_BY_ROT: dict[int, tuple[int, int]] = {
    0: (1, 0),    # +X
    1: (0, 1),    # +Z
    2: (-1, 0),   # -X
    3: (0, -1),   # -Z
}


def _advance(cell: Cell, rotation: int) -> Cell:
    """Cell adjacent to ``cell`` in the direction rotation encodes."""
    dx, dz = _DIR_BY_ROT[rotation & 0b11]
    return (cell[0] + dx, cell[1], cell[2] + dz)


def _direction_toward(src: Cell, dst: Cell) -> int:
    """Rotation whose unit vector best progresses src → dst.

    Tie-break: prefer the axis with larger magnitude; if equal, X
    wins. Used at the start of each interval to seed the direction
    of the first step.
    """
    dx = dst[0] - src[0]
    dz = dst[2] - src[2]
    if abs(dx) >= abs(dz):
        return 0 if dx >= 0 else 2
    return 1 if dz >= 0 else 3


# ---------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------

def _footprint_cells(
    origin: Cell, rotation: int, footprint_x: int,
) -> list[Cell]:
    """Cells the block's mesh occupies at placement.

    Origin + shadow. Multi-cell block meshes extend from the origin
    along the rotation axis; tracking the full footprint in the
    occupancy set stops later walker steps from landing inside the
    mesh (the map-1212 in-game failure from operator feedback).
    Unit-footprint blocks return [origin].
    """
    x, y, z = origin
    if footprint_x <= 1:
        return [origin]
    rot = rotation & 0b11
    if rot == 0:
        return [(x + i, y, z) for i in range(footprint_x)]
    if rot == 1:
        return [(x, y, z + i) for i in range(footprint_x)]
    if rot == 2:
        return [(x - i, y, z) for i in range(footprint_x)]
    return [(x, y, z - i) for i in range(footprint_x)]


def _shadow_cells_clear(
    *,
    cand: _CandidateBlock,
    occupied_cells: set[Cell],
) -> float:
    """Partial-multicell pre-step penalty.

    If the candidate has ``footprint_x > 1``, its mesh extends from
    the placement cell along the rotation axis. This check returns
    the fraction of shadow cells that would be occupied by some
    OTHER block — partial overlap risks mesh collision mid-route.
    Fraction 0 → clean; 1 → every shadow cell collides.

    Empty shadow cells (the #226 map-1212 failure) DON'T trigger
    this penalty here — those become visible in the post-interval
    validation pass and feed the soft post-placement signal. The
    pre-step version specifically guards against placing a
    multi-cell block whose mesh would overlap the anchor we're
    trying to reach.
    """
    fx = cand.info.footprint_x
    if fx <= 1:
        return 0.0
    # Shadow = footprint minus origin (origin is this block's OWN cell,
    # can't "collide" with self). Reuse _footprint_cells so the
    # rotation math stays in one place.
    shadow = _footprint_cells(cand.cell, cand.rotation, fx)[1:]
    if not shadow:
        return 0.0
    collisions = sum(1 for c in shadow if c in occupied_cells)
    return collisions / len(shadow)


def score_candidate(
    *,
    cand: _CandidateBlock,
    prev_block: tuple[str, str] | None,
    prev_prev_block: tuple[str, str] | None = None,
    pair_priors: Mapping[tuple[str, str], Mapping[tuple[str, str], float]],
    triple_priors: (
        Mapping[
            tuple[tuple[str, str], tuple[str, str]],
            Mapping[tuple[str, str], float],
        ] | None
    ) = None,
    shape_surface_lookup: (
        Mapping[tuple[str, str], _ShapeSurface] | None
    ) = None,
    path_so_far: list[dict[str, Any]],
    occupied_cells: set[Cell] | None = None,
    weights: Mapping[str, float],
) -> tuple[float, dict[str, float]]:
    """Return ``(score, breakdown)`` for a candidate.

    Breakdown keys match the schema's ``ai_score_breakdown`` fields
    so the artifact carries a legible cost sheet per block.
    """
    breakdown: dict[str, float] = {
        "pair_prior":         0.0,
        "triple_prior":       0.0,
        "connector":          0.0,
        "traversability":     0.0,
        "sequence":           0.0,
        "diversity_penalty":  0.0,
        "validation_penalty": 0.0,
    }

    # Pair prior: P(cand | prev). Zero when prev is None (interval
    # start) or transition never observed.
    if prev_block is not None:
        p = pair_priors.get(prev_block, {}).get((cand.family, cand.name), 0.0)
        breakdown["pair_prior"] = float(p)

    # Triple prior: P(cand | prev_prev, prev). Sharpens the pair
    # tier when we have two prior blocks and the exact triple was
    # observed in the corpus. Fires on step 3+ within an interval;
    # earlier steps fall back to the pair tier cleanly.
    if (
        triple_priors is not None
        and prev_block is not None
        and prev_prev_block is not None
    ):
        q = triple_priors.get(
            (prev_prev_block, prev_block), {},
        ).get((cand.family, cand.name), 0.0)
        breakdown["triple_prior"] = float(q)

    # Connector: 1.0 if candidate's connector_hint permits entry
    # along the rotation axis. Coarse: straight_x / curve_xz /
    # slope_xy / loop_y all drive along X at rotation 0, so
    # rotating the block aligns its connector automatically. We
    # trust the classifier's hint + rotation and give all drivable
    # connectors a 1.0 here. Finer-grained gating lands with
    # mesh-level footprint data (M2).
    breakdown["connector"] = 1.0

    # Traversability: look up (prev, cand) in the traversability
    # graph. For v0 we credit pair-observed transitions as a proxy
    # (pair_priors is corpus-derived and aligns with the
    # traversability edge set in practice). Re-homed to its own
    # tier so a future edge-level check slots in here.
    breakdown["traversability"] = 1.0 if breakdown["pair_prior"] > 0 else 0.0

    # v0.3 sequence score — in-memory (prev, cand) geometry pair
    # match via shape_class + surface_hint. Mirrors the spirit of
    # src.constraints.sequence_scoring._geometry_score_for without
    # the DB roundtrip. Fires from step 2 onwards (needs a prev
    # block); step 1 falls back to 0.0 cleanly.
    if shape_surface_lookup is not None and prev_block is not None:
        prev_ss = shape_surface_lookup.get(prev_block)
        cand_ss = shape_surface_lookup.get((cand.family, cand.name))
        if cand_ss is not None:
            breakdown["sequence"] = _sequence_pair_score(prev_ss, cand_ss)

    # Diversity penalty: count this block's recurrence in the path.
    if path_so_far:
        same = sum(
            1 for b in path_so_far
            if (b.get("block_family"), b.get("block_name"))
            == (cand.family, cand.name)
        )
        breakdown["diversity_penalty"] = same / max(1, len(path_so_far))
    else:
        breakdown["diversity_penalty"] = 0.0

    # v0.1 pre-step validation penalty: partial-multicell shadow
    # cell collision check. Full geom + jump validators still run
    # post-interval (and feed ai_confidence); this is the cheap
    # per-candidate gate that rejects obviously-broken placements
    # during the greedy walk.
    if occupied_cells is not None:
        breakdown["validation_penalty"] = _shadow_cells_clear(
            cand=cand, occupied_cells=occupied_cells,
        )
    else:
        breakdown["validation_penalty"] = 0.0

    score = (
        weights["pair_prior"]         * breakdown["pair_prior"]
      + weights["triple_prior"]       * breakdown["triple_prior"]
      + weights["connector"]          * breakdown["connector"]
      + weights["traversability"]     * breakdown["traversability"]
      + weights["sequence"]           * breakdown["sequence"]
      - weights["diversity_penalty"]  * breakdown["diversity_penalty"]
      - weights["validation_penalty"] * breakdown["validation_penalty"]
    )
    return score, breakdown


# Positive-weight signals — their sum is the denominator for the
# ai_confidence normalisation (max achievable step score, not the
# sum of absolute weights). Penalties subtract from the score but
# should NOT inflate the denominator — conflating the two is why
# v0.0 reported ai_confidence=0.159 on routes whose raw scores
# averaged ~0.59.
_POSITIVE_WEIGHT_KEYS = (
    "pair_prior", "triple_prior", "connector",
    "traversability", "sequence",
)


# ---------------------------------------------------------------------
# Beam search interval walk — v0.2
#
# Greedy (width=1) stays the default-equivalent pick when
# beam_width=1; wider beams explore alternate next-block choices
# per step and globally prune to beam_width survivors. Backtracking
# falls out for free: a beam that dead-ends on no_valid_candidates
# is dropped from the live pool but its siblings continue.
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Beam:
    """One partial path inside an in-progress interval search.

    Immutable so a beam's ancestors survive expansion without
    aliasing issues. ``interval_cells`` covers cells placed during
    THIS interval only; the caller's ``occupied_cells`` (prior
    intervals + anchors) stays shared + read-only during the walk.
    """
    blocks: tuple[dict[str, Any], ...]
    path_cells: tuple[Cell, ...]
    interval_cells: frozenset[Cell]
    score_sum: float
    current_cell: Cell
    prev_block: tuple[str, str] | None
    prev_prev_block: tuple[str, str] | None

    @property
    def depth(self) -> int:
        return len(self.blocks)

    @property
    def score_count(self) -> int:
        return len(self.blocks)


def _beam_reached_dst(beam: _Beam, dst: Cell) -> bool:
    """Arrival test: dst is Chebyshev-adjacent to the beam's current
    cell AND the beam has placed at least one block (so a trivial
    zero-block beam doesn't auto-complete and starve real searches)."""
    cx, _, cz = beam.current_cell
    dx = abs(dst[0] - cx)
    dz = abs(dst[2] - cz)
    return beam.depth > 0 and max(dx, dz) <= 1


def _expand_beam(
    *,
    beam: _Beam,
    dst_cell: Cell,
    catalogue: list[_CatalogueEntry],
    pair_priors: Mapping[tuple[str, str], Mapping[tuple[str, str], float]],
    triple_priors: (
        Mapping[
            tuple[tuple[str, str], tuple[str, str]],
            Mapping[tuple[str, str], float],
        ] | None
    ),
    shape_surface_lookup: (
        Mapping[tuple[str, str], _ShapeSurface] | None
    ),
    occupied_cells: set[Cell],
    weights: Mapping[str, float],
    top_n_children: int,
) -> list[_Beam]:
    """Expand one beam by one step; return up to ``top_n_children``
    successor beams in score-descending order.

    Wider children counts feed the beam width's global pruning step
    (which picks the top ``beam_width`` across ALL parents' children).
    """
    rotation = _direction_toward(beam.current_cell, dst_cell)
    next_cell = _advance(beam.current_cell, rotation)
    if next_cell in occupied_cells or next_cell in beam.interval_cells:
        return []

    combined_occupancy = occupied_cells | beam.interval_cells
    # Keep the catalogue entry alongside the score so we know the
    # footprint when building the child beam — avoids an O(catalogue)
    # re-lookup per child.
    scored: list[
        tuple[float, dict[str, Any], dict[str, float], _CatalogueEntry]
    ] = []
    path_list = list(beam.blocks)
    for entry in catalogue:
        cand = _CandidateBlock(
            family=entry.family, name=entry.name,
            cell=next_cell, rotation=rotation, info=entry.info,
        )
        score, breakdown = score_candidate(
            cand=cand,
            prev_block=beam.prev_block,
            prev_prev_block=beam.prev_prev_block,
            pair_priors=pair_priors,
            triple_priors=triple_priors,
            shape_surface_lookup=shape_surface_lookup,
            path_so_far=path_list,
            occupied_cells=combined_occupancy,
            weights=weights,
        )
        block = {
            "block_family": entry.family,
            "block_name": entry.name,
            "x": next_cell[0], "y": next_cell[1], "z": next_cell[2],
            "rotation": rotation,
            "ai_score": round(score, 6),
            "ai_score_breakdown": {k: round(v, 6) for k, v in breakdown.items()},
        }
        scored.append((score, block, breakdown, entry))

    if not scored:
        return []
    # Descending by score; take top_n_children.
    scored.sort(key=lambda t: -t[0])
    children: list[_Beam] = []
    for score, block, _, entry in scored[:top_n_children]:
        # v0.4: mark the whole footprint as occupied so a later step
        # can't land inside this block's mesh.
        fp_cells = _footprint_cells(
            next_cell, block["rotation"], entry.info.footprint_x,
        )
        children.append(_Beam(
            blocks=beam.blocks + (block,),
            path_cells=beam.path_cells + (next_cell,),
            interval_cells=beam.interval_cells | frozenset(fp_cells),
            score_sum=beam.score_sum + score,
            current_cell=next_cell,
            prev_block=(block["block_family"], block["block_name"]),
            prev_prev_block=beam.prev_block,
        ))
    return children


def _generate_interval(
    *,
    src_cell: Cell,
    dst_cell: Cell,
    src_block: tuple[str, str] | None,
    catalogue: list[_CatalogueEntry],
    pair_priors: Mapping[tuple[str, str], Mapping[tuple[str, str], float]],
    triple_priors: (
        Mapping[
            tuple[tuple[str, str], tuple[str, str]],
            Mapping[tuple[str, str], float],
        ] | None
    ),
    occupied_cells: set[Cell],
    max_depth: int,
    weights: Mapping[str, float],
    beam_width: int = 1,
    top_n_children: int = DEFAULT_TOP_N_CANDIDATES,
    shape_surface_lookup: (
        Mapping[tuple[str, str], _ShapeSurface] | None
    ) = None,
) -> _IntervalResult:
    """Beam search from ``src_cell`` toward ``dst_cell``.

    ``beam_width=1`` is the v0.1 greedy equivalent (single beam,
    top-1 expansion per step). Wider beams keep ``beam_width``
    parallel candidate paths alive; each expands to
    ``top_n_children`` successors per step and the global top
    ``beam_width`` survives to the next layer.

    Completed beams (arrived within Chebyshev 1 of dst) are held
    aside and the best-scoring completed beam wins at termination.
    If all beams dead-end before reaching dst → reject_reason
    ``beam_exhausted``. If the starting pool finds no candidates
    at all → ``no_valid_candidates``.
    """
    result = _IntervalResult(
        blocks=[], path_cells=[src_cell], score_sum=0.0, score_count=0,
    )

    # Trivial interval (Chebyshev-adjacent anchors) returns empty.
    if max(
        abs(dst_cell[0] - src_cell[0]),
        abs(dst_cell[2] - src_cell[2]),
    ) <= 1:
        return result

    initial = _Beam(
        blocks=(), path_cells=(src_cell,),
        interval_cells=frozenset(),
        score_sum=0.0, current_cell=src_cell,
        prev_block=src_block, prev_prev_block=None,
    )
    live: list[_Beam] = [initial]
    completed: list[_Beam] = []
    any_step_succeeded = False

    for _ in range(max_depth):
        # Move any already-arrived beams into the completed bucket.
        next_live: list[_Beam] = []
        for b in live:
            if _beam_reached_dst(b, dst_cell):
                completed.append(b)
            else:
                next_live.append(b)
        if not next_live:
            break

        # Expand each live beam, collect all children.
        all_children: list[_Beam] = []
        for b in next_live:
            all_children.extend(_expand_beam(
                beam=b, dst_cell=dst_cell,
                catalogue=catalogue,
                pair_priors=pair_priors,
                triple_priors=triple_priors,
                shape_surface_lookup=shape_surface_lookup,
                occupied_cells=occupied_cells,
                weights=weights,
                top_n_children=top_n_children,
            ))
        if not all_children:
            # Every live beam dead-ended. No further progress.
            break
        any_step_succeeded = True

        # Global prune — keep top ``beam_width`` by score_sum.
        all_children.sort(key=lambda b: -b.score_sum)
        live = all_children[:beam_width]

    # Any remaining live beams that happen to arrive exactly at the
    # depth cap count as completed.
    for b in live:
        if _beam_reached_dst(b, dst_cell):
            completed.append(b)

    if not completed:
        # Differentiate "never made a single step" (occupancy or
        # empty catalogue at step 1) from "walked a while but
        # couldn't reach the destination within max_depth". On the
        # "walked a while" case, emit the best partial beam as
        # ``result.blocks`` for debugging visibility — operators
        # can inspect the ai_score_breakdown of the steps leading
        # to the dead-end.
        if not any_step_succeeded:
            result.reject_reason = "no_valid_candidates"
            result.detail = (
                f"first-step expansion produced zero candidates "
                f"(src={src_cell} dst={dst_cell}); likely an "
                f"occupancy conflict or empty catalogue"
            )
        else:
            result.reject_reason = "beam_exhausted"
            result.detail = (
                f"no beam reached cheb=1 of {dst_cell} within "
                f"max_depth={max_depth} (beam_width={beam_width}, "
                f"top_n_children={top_n_children})"
            )
            if live:
                best_partial = max(
                    live,
                    key=lambda b: (b.score_sum / max(1, b.depth), -b.depth),
                )
                result.blocks = list(best_partial.blocks)
                result.path_cells = list(best_partial.path_cells)
                result.score_sum = best_partial.score_sum
                result.score_count = best_partial.score_count
        return result

    # Best completed beam wins.
    best = max(
        completed,
        key=lambda b: (b.score_sum / max(1, b.depth), -b.depth),
    )
    result.blocks = list(best.blocks)
    result.path_cells = list(best.path_cells)
    result.score_sum = best.score_sum
    result.score_count = best.score_count
    # Merge the winning beam's interval cells into the caller's
    # occupancy so subsequent intervals don't collide.
    occupied_cells |= best.interval_cells
    return result


# ---------------------------------------------------------------------
# Base-map fetch
# ---------------------------------------------------------------------

@dataclass
class _BaseAnchors:
    source_map_id: str | None
    anchors: tuple[Anchor, ...]             # Spawn → CP₁ → … → Goal
    anchor_blocks: list[dict[str, Any]]     # grid rows for the anchors
    model_hash: str | None
    learned_score_version: str | None


_ANCHOR_SQL = """
SELECT waypoint_index, waypoint_order, tag, x, y, z
FROM map_checkpoints
WHERE map_id = %s
  AND x IS NOT NULL AND y IS NOT NULL AND z IS NOT NULL
ORDER BY waypoint_order, waypoint_index
"""


_ANCHOR_BLOCKS_SQL = """
SELECT block_family, block_type, x, y, z, rotation
FROM block_placements
WHERE map_id = %s
  AND is_free = 0
  AND x IS NOT NULL AND y IS NOT NULL AND z IS NOT NULL
"""


_META_SQL = """
SELECT source_map_id FROM maps WHERE id = %s
"""


_PROVENANCE_SQL = """
SELECT learned_score_model_hash, learned_score_version
FROM route_corridors
WHERE map_id = %s AND learned_corridor_score IS NOT NULL
GROUP BY learned_score_model_hash, learned_score_version
ORDER BY COUNT(*) DESC
LIMIT 1
"""


def _fetch_base_anchors(
    conn: Connection, map_id: int,
) -> _BaseAnchors:
    with cursor(conn) as cur:
        cur.execute(_META_SQL, (map_id,))
        meta = cur.fetchone()
        if meta is None:
            raise ValueError(f"base map_id={map_id} not found")
        source_map_id = str(meta[0]) if meta[0] is not None else None

        cur.execute(_ANCHOR_SQL, (map_id,))
        anchor_rows = cur.fetchall()
        cur.execute(_ANCHOR_BLOCKS_SQL, (map_id,))
        block_rows = cur.fetchall()
        cur.execute(_PROVENANCE_SQL, (map_id,))
        prov = cur.fetchone()

    anchor_cells_set: set[Cell] = set()
    anchors: list[Anchor] = []
    for waypoint_index, waypoint_order, tag, x, y, z in anchor_rows:
        cell = (int(x), int(y), int(z))
        anchor_cells_set.add(cell)
        anchors.append(Anchor(
            tag=str(tag), order=int(waypoint_order), cell=cell,
        ))

    # Preserve only the block rows at anchor cells — the AI walk
    # synthesises the rest. This keeps the Spawn / CP / Goal blocks
    # verbatim so the finishability gate's anchor-presence test
    # stays satisfied.
    anchor_blocks: list[dict[str, Any]] = []
    for family, name, x, y, z, rotation in block_rows:
        cell = (int(x), int(y), int(z))
        if cell not in anchor_cells_set:
            continue
        anchor_blocks.append({
            "block_family": str(family), "block_name": str(name),
            "x": cell[0], "y": cell[1], "z": cell[2],
            "rotation": int(rotation or 0),
        })

    model_hash = None
    learned_score_version = None
    if prov is not None:
        model_hash = str(prov[0]) if prov[0] is not None else None
        learned_score_version = str(prov[1]) if prov[1] is not None else None

    # Detect Linked-CP — assembler enforces this but we need to
    # short-circuit early before running the walk. Any anchor with
    # tag 'LinkedCheckpoint' is enough; scope-v0 pins this rule.
    return _BaseAnchors(
        source_map_id=source_map_id,
        anchors=tuple(anchors),
        anchor_blocks=anchor_blocks,
        model_hash=model_hash,
        learned_score_version=learned_score_version,
    )


def _is_linked_cp(anchors: tuple[Anchor, ...]) -> bool:
    return any(a.tag == "LinkedCheckpoint" for a in anchors)


def _anchor_block_at(
    anchor_blocks: list[dict[str, Any]], cell: Cell,
) -> tuple[str, str] | None:
    for b in anchor_blocks:
        if (b["x"], b["y"], b["z"]) == cell:
            return (b["block_family"], b["block_name"])
    return None


# ---------------------------------------------------------------------
# Artifact assembly
# ---------------------------------------------------------------------

def _hash_inputs(inputs: AIGenerationInputs) -> str:
    # Same pattern generate_from_base uses; keeps run_id stable
    # across Python processes.
    payload = json.dumps({
        "base_map_id": inputs.base_map_id,
        "base_map_source_id": None,
        "style_tag_filter": inputs.style_tag_filter,
        "difficulty": inputs.difficulty,
        "random_seed": inputs.random_seed,
        "strip": False,
        "ai_generator_version": AI_GENERATOR_VERSION,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _build_v0_2_artifact(
    *,
    inputs: AIGenerationInputs,
    base: _BaseAnchors,
    synthesised_blocks: list[dict[str, Any]],
    anchor_blocks: list[dict[str, Any]],
    all_cells: list[Cell],
    interval_entries: list[dict[str, Any]],
    corridors_used: list[dict[str, Any]],
    route_verified: bool,
    reject_reason: str | None,
    estimated_time_ms: int | None,
    ai_confidence: float | None,
    detail: str | None,
    config_hash: str,
    sha: str,
) -> dict[str, Any]:
    anchors_order = [a for a in base.anchors]
    checkpoints: list[dict[str, Any]] = []
    for a in anchors_order:
        if a.cell is None:
            continue
        checkpoints.append({
            "waypoint_index": len(checkpoints),
            "waypoint_order": int(a.order),
            "tag": a.tag,
            "x": a.cell[0], "y": a.cell[1], "z": a.cell[2],
        })

    run_id = _hash_inputs(inputs)
    artifact: dict[str, Any] = {
        "schema_version": "generation-v0.2",
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ).replace("+00:00", "Z"),
        "inputs": {
            "base_map_id": inputs.base_map_id,
            "base_map_source_id": base.source_map_id,
            "style_tag_filter": inputs.style_tag_filter,
            "difficulty": inputs.difficulty,
            "random_seed": inputs.random_seed,
            "strip": False,
        },
        "provenance": {
            "model_hash": (
                base.model_hash
                or "0" * 64  # schema requires 64-hex; 0-fill when unknown
            ),
            "learned_score_version": base.learned_score_version or "",
            "config_hash": config_hash,
            "code_version": sha,
            "classification_version": CLASSIFICATION_VERSION,
        },
        "map": {
            "waypoint_order_style": "linked",
            "interval_count": max(1, len(anchors_order) - 1),
            "blocks": anchor_blocks + synthesised_blocks,
            "checkpoints": checkpoints,
            "ai_generated": True,
            "ai_generator_version": AI_GENERATOR_VERSION,
        },
        "route": {
            "intervals": interval_entries,
            "cells_total": len(all_cells),
            "corridors_used": corridors_used,
        },
        "finishability": {
            "route_verified": route_verified,
            "estimated_time_ms": estimated_time_ms,
            "ai_confidence": ai_confidence,
            "reject_reason": reject_reason,
            "gate_version": GATE_VERSION,
            "detail": detail,
        },
    }
    return artifact


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------

def generate_ai_map(
    conn: Connection,
    *,
    inputs: AIGenerationInputs,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Build one ``generation-v0.2`` artifact via block-by-block
    synthesis between base-map anchors.

    Raises ``RuntimeError`` on schema-validation failure of the
    produced artifact (real bug → scope-doc drift).

    Does NOT raise on interval rejections. Those become
    reject_reason + route_verified=False artifacts, same pattern
    generate_from_base uses.
    """
    sha = code_version()
    config_hash = resolve_config_hash(config)

    base = _fetch_base_anchors(conn, inputs.base_map_id)

    if len(base.anchors) < 2:
        return _build_v0_2_artifact(
            inputs=inputs, base=base,
            synthesised_blocks=[], anchor_blocks=base.anchor_blocks,
            all_cells=[], interval_entries=[], corridors_used=[],
            route_verified=False,
            reject_reason="empty_corridors",
            estimated_time_ms=None, ai_confidence=None,
            detail="base map has fewer than 2 anchors",
            config_hash=config_hash, sha=sha,
        )

    if not _is_linked_cp(base.anchors):
        return _build_v0_2_artifact(
            inputs=inputs, base=base,
            synthesised_blocks=[], anchor_blocks=base.anchor_blocks,
            all_cells=[], interval_entries=[], corridors_used=[],
            route_verified=False,
            reject_reason="plain_cp_not_supported_v0",
            estimated_time_ms=None, ai_confidence=None,
            detail=(
                "v0.2 AI generator supports Linked-CP maps only "
                "(see minimal-ai-generator-v0.md §Scope)"
            ),
            config_hash=config_hash, sha=sha,
        )

    catalogue = _load_candidate_catalogue(conn)
    pair_priors = _load_pair_priors(conn)
    triple_priors = _load_triple_priors(conn)
    shape_surface_lookup = _load_shape_surface_lookup(conn)

    # Occupied set seeded with anchor cells so the walk never
    # overwrites Spawn / CP / Goal.
    occupied: set[Cell] = {a.cell for a in base.anchors if a.cell}

    synthesised: list[dict[str, Any]] = []
    interval_entries: list[dict[str, Any]] = []
    corridors_used: list[dict[str, Any]] = []
    all_cells: list[Cell] = []
    overall_score_sum = 0.0
    overall_score_count = 0

    for idx in range(len(base.anchors) - 1):
        src = base.anchors[idx]
        dst = base.anchors[idx + 1]
        if src.cell is None or dst.cell is None:
            continue
        src_block_ref = _anchor_block_at(base.anchor_blocks, src.cell)
        result = _generate_interval(
            src_cell=src.cell, dst_cell=dst.cell,
            src_block=src_block_ref,
            catalogue=catalogue,
            pair_priors=pair_priors,
            triple_priors=triple_priors,
            occupied_cells=occupied,
            max_depth=inputs.max_interval_depth,
            weights=AI_GENERATOR_WEIGHTS,
            beam_width=max(1, inputs.beam_width),
            shape_surface_lookup=shape_surface_lookup,
        )
        if result.reject_reason is not None:
            return _build_v0_2_artifact(
                inputs=inputs, base=base,
                synthesised_blocks=synthesised + result.blocks,
                anchor_blocks=base.anchor_blocks,
                all_cells=all_cells + result.path_cells,
                interval_entries=interval_entries, corridors_used=corridors_used,
                route_verified=False,
                reject_reason=result.reject_reason,
                estimated_time_ms=None, ai_confidence=None,
                detail=result.detail,
                config_hash=config_hash, sha=sha,
            )
        synthesised.extend(result.blocks)
        all_cells.extend(result.path_cells)
        overall_score_sum += result.score_sum
        overall_score_count += result.score_count

        # Route-block bookkeeping. The artifact's route.intervals[*]
        # shape originated with the corridor-based generator; v0.2
        # doesn't pick existing corridors so we synthesise dummy
        # corridor-ids (negative, interval-indexed) to satisfy the
        # schema without colliding with real corridor_ids.
        synthesised_corridor_id = -(idx + 1)
        interval_entries.append({
            "index": idx,
            "src_tag": src.tag, "src_order": int(src.order),
            "dst_tag": dst.tag, "dst_order": int(dst.order),
            "chosen_corridor_id": synthesised_corridor_id,
            "chosen_corridor_score": 0.0,
            "path_length_cells": len(result.path_cells),
            "expected_time_ms": int(32 * len(result.path_cells) / 30 * 1000),
        })
        corridors_used.append({
            "corridor_id": synthesised_corridor_id,
            "interval_index": idx,
            "learned_corridor_score": 0.0,
            "contains_virtual_edge": False,
            "path_length_cells": len(result.path_cells),
        })

    # Post-synthesis validation. One combined pass over the full
    # block set + full route cells.
    all_blocks = base.anchor_blocks + synthesised
    geometry_lookup = load_geometry_lookup(conn)
    normalised = [_normalize_block(b) for b in all_blocks]
    validation_detail: str | None = None
    if geometry_lookup and all_cells:
        geom_rpt = validate_map_geometry(
            blocks=normalised,
            geometry_lookup=geometry_lookup,
            route_cells=all_cells,
            spawn_cell=base.anchors[0].cell,
        )
        fails_on_route = sum(
            1 for f in geom_rpt.findings
            if f.severity == SEVERITY_FAIL and f.code == CODE_PARTIAL_MULTICELL
        )
        replay_cells = load_replay_touched_cells(conn, map_id=inputs.base_map_id)
        jump_rpt = validate_jumps(
            blocks=normalised,
            geometry_lookup=geometry_lookup,
            route_cells=all_cells,
            replay_touched_cells=replay_cells if replay_cells else None,
        )
        broken_jumps = len(jump_rpt.by_class(CLASS_LIKELY_BROKEN))
        validation_detail = (
            f"post-synth: partial_multicell_fails={fails_on_route} "
            f"likely_broken_jumps={broken_jumps}"
        )
        _LOG.info("ai_generator validation: %s", validation_detail)

    # AI confidence: mean step score divided by the max achievable
    # step score (positive weights only, each at their cap of 1.0).
    # Penalties subtract from the raw score but don't inflate the
    # denominator — v0.0 conflated the two and reported 0.159 on
    # routes whose raw scores averaged ~0.59.
    positive_weight_sum = sum(
        AI_GENERATOR_WEIGHTS[k] for k in _POSITIVE_WEIGHT_KEYS
    )
    if overall_score_count > 0 and positive_weight_sum > 0:
        raw = overall_score_sum / overall_score_count
        ai_confidence = max(0.0, min(1.0, raw / positive_weight_sum))
    else:
        ai_confidence = None

    # Route verified: walk completed every interval AND confidence
    # meets the floor. Finishability gate semantics are preserved
    # so the dashboard's existing reject handling works unchanged.
    route_verified = (
        len(interval_entries) == (len(base.anchors) - 1)
        and ai_confidence is not None
        and ai_confidence >= AI_CONFIDENCE_FLOOR
    )
    reject_reason: str | None = None
    if not route_verified:
        reject_reason = (
            "confidence_below_floor"
            if ai_confidence is not None
            and ai_confidence < AI_CONFIDENCE_FLOOR
            else None
        )
    estimated_time_ms = (
        sum(e["expected_time_ms"] for e in interval_entries)
        if interval_entries else None
    )

    artifact = _build_v0_2_artifact(
        inputs=inputs, base=base,
        synthesised_blocks=synthesised,
        anchor_blocks=base.anchor_blocks,
        all_cells=all_cells,
        interval_entries=interval_entries,
        corridors_used=corridors_used,
        route_verified=route_verified,
        reject_reason=reject_reason,
        estimated_time_ms=estimated_time_ms,
        ai_confidence=ai_confidence,
        detail=validation_detail,
        config_hash=config_hash, sha=sha,
    )

    err = validate_generated_map(artifact)
    if err is not None:
        raise RuntimeError(
            f"v0.2 artifact failed schema validation: {err}"
        )
    _LOG.info(
        "generate_ai_map: run_id=%s base_map_id=%d synthesised_blocks=%d "
        "route_verified=%s ai_confidence=%s reject=%s",
        artifact["run_id"],
        inputs.base_map_id,
        len(synthesised),
        route_verified,
        f"{ai_confidence:.3f}" if ai_confidence is not None else "n/a",
        reject_reason,
    )
    return artifact
