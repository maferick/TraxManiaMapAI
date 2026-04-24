"""Route assembly per ``docs/generation/generation-scope-v0.md`` §
Route assembly.

Two entry points:

- :func:`assemble_route_from_inputs` — pure on already-fetched data
  (anchors + candidate corridors). Ideal for unit tests and for
  future callers that hold their inputs in memory (e.g. an
  in-process generator that just produced fresh corridors).
- :func:`assemble_route` — DB wrapper that materialises the inputs
  from MariaDB, then delegates to the pure function.

Both return ``AssembledRoute | AssemblyError`` — never a partial
route. The caller hands the result to
:func:`src.generation.finishability.run_finishability_gate` to get
the operator-facing verdict.

Tie-breaks + continuity checks are documented in the scope doc and
pinned here by the ``_TIE_BREAK_KEY`` + ``_cells_continuous``
helpers so reviewers can diff implementations against the spec.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Callable, Sequence

from pymysql.connections import Connection

# Import the same physics constants the label builder uses so
# estimated_time is consistent across label-time + gate-time. The
# scope doc explicitly calls this out ("Don't redefine these
# constants in the generator — import from the existing module.
# Keeps physics consistent between label-time and gate-time.").
from src.corridor.ranking.time_envelope_labels import (
    _BLOCK_SIZE_M,
    _DEFAULT_SPEED_PRIOR_M_S,
)
from src.corridor.traversability.classification import CLASSIFICATION_VERSION
from src.generation.types import (
    Anchor,
    AssembledRoute,
    AssemblyError,
    Cell,
    ChosenCorridor,
    IntervalAssembly,
)
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Inputs to the pure assembly pass — kept in one dataclass so the DB
# wrapper has a single object to build and the unit tests a single
# object to construct.
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class CandidateCorridor:
    """One row from route_corridors, shaped for assembly. Order within
    an interval is not meaningful; the algorithm sorts them.

    ``combined_sequence_score`` carries the #218-5 pattern+geometry
    average (0..1 or None when the corpus hasn't been scored yet).
    It participates as a tier-below tie-break after
    learned_corridor_score; see ``_tie_break_key``.

    ``validation_score`` carries the #226/#227 per-corridor geom +
    jump validator score (0..1 or None when no validator was run).
    It joins the tie-break right after ``learned_corridor_score`` so
    the learned ranking stays authoritative but broken-geometry
    corridors get demoted (and zero-score corridors are pushed to
    the back of the pool entirely). None treated as "no info" — it
    sorts equal with low-but-positive scores so the pipeline
    degrades gracefully when no validator is wired."""
    corridor_id: int
    map_id: int
    src: Anchor
    dst: Anchor
    path_cells: tuple[Cell, ...]
    path_length: int
    contains_virtual_edge: bool
    corridor_confidence: float | None
    learned_corridor_score: float | None
    combined_sequence_score: float | None = None
    validation_score: float | None = None


@dataclass(frozen=True)
class AssemblyInputs:
    """Pure inputs for :func:`assemble_route_from_inputs`. Keeps the
    function signature small + testable without fake-DB plumbing.

    ``random_seed`` + ``top_k_candidates`` drive Level-1 mutation: per
    interval, the algorithm picks from the top-K tie-break-ordered
    candidates instead of always the top-1. The pick is a deterministic
    function of seed + interval index, so a given (seed, k, corpus)
    tuple produces the same route every time. Defaults preserve
    Phase-1 behaviour (k=1 → always rank-1).

    ``use_validation_tie_break`` toggles the #226/#227 validation-
    aware tie-break. When False (default), ``validation_score`` is
    ignored and the original (learned, combined_sequence, length,
    id) cascade applies. When True, validation_score injects a new
    tier right after learned_corridor_score: zero-score corridors
    get pushed to the back of the pool, higher validation_score
    wins ties. Callers that haven't populated ``validation_score``
    on candidates see no behaviour change.
    """
    map_id: int
    is_linked_cp: bool
    anchors: tuple[Anchor, ...]          # Spawn → CP₁ → … → Goal
    candidates: tuple[CandidateCorridor, ...]
    random_seed: int = 0
    top_k_candidates: int = 1
    use_validation_tie_break: bool = False


# ---------------------------------------------------------------------
# Physics
# ---------------------------------------------------------------------

def _expected_time_ms(path_length_cells: int) -> int:
    """Per-corridor expected completion time, scope-v0 §Route assembly:
        expected_time_ms = path_length_cells * BLOCK_SIZE_M / SPEED_PRIOR_M_S * 1000
    Uses the same constants as the time_envelope label so label-time
    and gate-time don't drift."""
    if path_length_cells <= 0:
        return 0
    seconds = (path_length_cells * _BLOCK_SIZE_M) / _DEFAULT_SPEED_PRIOR_M_S
    return int(round(seconds * 1000.0))


# ---------------------------------------------------------------------
# Tie-break + continuity
# ---------------------------------------------------------------------

def _tie_break_key(c: CandidateCorridor) -> tuple:
    """Sort key used to pick the top candidate per interval.

    Tiers, in order:
      1. Highest ``learned_corridor_score``. This is the trained
         corridor-ranking model's verdict; it already bakes in
         traversability-derived evidence and is the authoritative
         rank signal.
      2. Highest ``combined_sequence_score`` (#218-5). Breaks
         learned-score ties by preferring corridors whose block
         transitions match the corpus's pattern/geometry priors.
         NULL treated as -1 so un-scored corridors lose to scored
         ones on equal footing.
      3. Shorter ``path_length``.
      4. Lower ``corridor_id`` (final deterministic tiebreak).

    This cascade is purely additive relative to scope-v0: when
    combined_sequence_score is NULL everywhere (pre-#218), the
    second tier collapses to a constant and behaviour reduces to
    the original (learned, length, id) order."""
    return (
        -(c.learned_corridor_score if c.learned_corridor_score is not None else -1.0),
        -(c.combined_sequence_score if c.combined_sequence_score is not None else -1.0),
        c.path_length,
        c.corridor_id,
    )


def _fmt_score(v: float | None) -> str:
    """Compact score formatter for log lines — 'None' vs '0.350'."""
    return "None" if v is None else f"{v:.3f}"


def _tie_break_key_with_validation(c: CandidateCorridor) -> tuple:
    """Validation-aware tie-break. Invariants vs :func:`_tie_break_key`:

      1. Highest ``learned_corridor_score`` still wins — the trained
         model stays authoritative.
      2. NEW: zero-floor tier. validation_score == 0.0 sorts to the
         back of the pool within a learned-score bucket ('avoid
         corridors with validation_score = 0' per the PR ask).
         None / positive scores tie on this tier.
      3. NEW: higher ``validation_score`` preferred. None treated as
         -1 (un-validated corridors lose to validated ones with
         positive scores; behaviour identical to combined_sequence
         tier when nothing is scored).
      4. Highest ``combined_sequence_score`` — unchanged.
      5. Shorter ``path_length`` — unchanged.
      6. Lower ``corridor_id`` — unchanged.

    Float-equality pitfall: exact 0.0 from :func:`_corridor_validation_score`
    happens via clamp(), not via a floating-point subtraction landing
    at 0.0, so the == 0.0 comparison is safe for the values this
    pipeline produces. If a future formula starts emitting 0.0 via
    arithmetic, swap to ``<= _ZERO_EPSILON``."""
    vs = c.validation_score
    zero_floor = 1 if (vs is not None and vs == 0.0) else 0
    return (
        -(c.learned_corridor_score if c.learned_corridor_score is not None else -1.0),
        zero_floor,
        -(vs if vs is not None else -1.0),
        -(c.combined_sequence_score if c.combined_sequence_score is not None else -1.0),
        c.path_length,
        c.corridor_id,
    )


def _pick_within_top_k(
    *, random_seed: int, interval_index: int, pool_size: int, top_k: int,
) -> int:
    """Pick an index in ``[0, min(top_k, pool_size))`` deterministically
    from ``(random_seed, interval_index)``. Used by Level-1 mutation to
    select among the top-K tie-break-ordered candidates per interval.

    Determinism matters — two different Python processes must produce
    the same pick for the same (seed, index, pool, k) tuple, or the
    artifact's ``run_id`` becomes a lie. ``hash()`` is per-process
    salted, so we use ``blake2b`` over a short deterministic string.
    """
    k = min(max(1, top_k), max(1, pool_size))
    if k <= 1:
        return 0
    payload = f"{random_seed}:{interval_index}".encode("utf-8")
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, "big") % k


def _cells_continuous(end_cell: Cell, start_cell: Cell) -> bool:
    """Chain-continuity test from scope-v0:
        C_i's last cell is adjacent to C_{i+1}'s first cell,
        OR they share an anchor block (the CP block itself).
    Interpret ``adjacent`` as Chebyshev distance <= 1 (same cell
    counts as adjacent — covers the shared-anchor case)."""
    dx = abs(end_cell[0] - start_cell[0])
    dy = abs(end_cell[1] - start_cell[1])
    dz = abs(end_cell[2] - start_cell[2])
    return max(dx, dy, dz) <= 1


# ---------------------------------------------------------------------
# Pure assembly pass
# ---------------------------------------------------------------------

def assemble_route_from_inputs(
    inputs: AssemblyInputs,
) -> AssembledRoute | AssemblyError:
    """Pure function: produce an :class:`AssembledRoute` from already-
    fetched inputs, or an :class:`AssemblyError` if any gate fails.

    Applies the algorithm from scope-v0 step by step:

      1. Require Linked-CP; plain-CP short-circuits.
      2. Per interval, filter candidates to NOT NULL learned score,
         pick top by the pinned tie-break.
      3. Assert chain continuity between consecutive chosen corridors.
      4. Sum expected times; mean learned score for AI confidence.
    """
    # 0. Sanity on anchors themselves. An empty anchor sequence or a
    #    sequence without at least Spawn + Goal yields empty_corridors
    #    because we can't even form one interval.
    if len(inputs.anchors) < 2:
        return AssemblyError(
            reason="empty_corridors",
            detail=(
                f"anchor sequence has {len(inputs.anchors)} entries; "
                "need at least Spawn + Goal"
            ),
        )

    # 1. Linked-CP guard. Plain-CP short-circuits per scope-v0.
    if not inputs.is_linked_cp:
        return AssemblyError(
            reason="plain_cp_not_supported_v0",
            detail=(
                "v0 generation supports Linked-CP maps only; plain-CP "
                "interval ordering is ambiguous until per-CP alignment "
                "or OpenPlanet telemetry arrives"
            ),
        )

    # No corridor candidates at all → empty_corridors.
    if not inputs.candidates:
        return AssemblyError(
            reason="empty_corridors",
            detail="map has no route_corridors rows",
        )

    # Index candidates by (src_tag, src_order, dst_tag, dst_order) so
    # per-interval filtering is a cheap dict lookup.
    candidates_by_interval: dict[
        tuple[str, int, str, int], list[CandidateCorridor]
    ] = {}
    for c in inputs.candidates:
        key = (c.src.tag, c.src.order, c.dst.tag, c.dst.order)
        candidates_by_interval.setdefault(key, []).append(c)

    # 2. Walk anchor pairs, pick the top candidate per interval.
    intervals: list[IntervalAssembly] = []
    chosen_corridors: list[ChosenCorridor] = []
    for idx in range(len(inputs.anchors) - 1):
        src = inputs.anchors[idx]
        dst = inputs.anchors[idx + 1]
        pool = candidates_by_interval.get(
            (src.tag, src.order, dst.tag, dst.order), [],
        )
        scored_pool = [c for c in pool if c.learned_corridor_score is not None]
        if not scored_pool:
            return AssemblyError(
                reason="missing_corridor_in_interval",
                detail=(
                    f"no learned-scored corridor for interval "
                    f"{src.tag}#{src.order} → {dst.tag}#{dst.order}"
                ),
                interval_index=idx,
            )
        # Record pre-validator ordering so we can log swaps when the
        # validation-aware tie-break flips the selection. Cheap — we
        # already need the learned-only sort as the fallback anchor.
        scored_pool.sort(key=_tie_break_key)
        pre_validator_top = scored_pool[0]

        if inputs.use_validation_tie_break:
            scored_pool.sort(key=_tie_break_key_with_validation)

        pick_index = _pick_within_top_k(
            random_seed=inputs.random_seed,
            interval_index=idx,
            pool_size=len(scored_pool),
            top_k=inputs.top_k_candidates,
        )
        top = scored_pool[pick_index]
        assert top.learned_corridor_score is not None  # narrowed by filter

        # Log when the validator rewrote the top-1. Only emit on true
        # swaps (pick_index==0 with a different corridor_id than the
        # learned-only top); Level-1 mutation picks below index 0 are
        # deliberate per-seed variation, not validator interference.
        if (
            inputs.use_validation_tie_break
            and pick_index == 0
            and top.corridor_id != pre_validator_top.corridor_id
        ):
            _LOG.info(
                "validator_swap: interval=%d learned_top=%d(learned=%.3f,"
                "val=%s) → validator_top=%d(learned=%.3f,val=%s)",
                idx,
                pre_validator_top.corridor_id,
                float(pre_validator_top.learned_corridor_score or -1.0),
                _fmt_score(pre_validator_top.validation_score),
                top.corridor_id,
                float(top.learned_corridor_score or -1.0),
                _fmt_score(top.validation_score),
            )

        chosen = ChosenCorridor(
            corridor_id=top.corridor_id,
            map_id=top.map_id,
            src=top.src,
            dst=top.dst,
            path_cells=top.path_cells,
            path_length=top.path_length,
            contains_virtual_edge=top.contains_virtual_edge,
            corridor_confidence=top.corridor_confidence,
            learned_corridor_score=float(top.learned_corridor_score),
            expected_time_ms=_expected_time_ms(top.path_length),
            combined_sequence_score=top.combined_sequence_score,
            validation_score=top.validation_score,
        )
        chosen_corridors.append(chosen)
        intervals.append(IntervalAssembly(
            index=idx, src=src, dst=dst, chosen=chosen,
        ))

    # 3. Chain continuity. scope-v0: "adjacent OR share an anchor
    #    block." A multi-cell CP (common — parser emits one row per
    #    cell of a multi-cell gate) lets corridor i end at one cell of
    #    the anchor and corridor i+1 start at a different cell of the
    #    same anchor; Chebyshev distance can exceed 1 in that case but
    #    the anchor is still shared by construction (both corridors
    #    point at the same (tag, waypoint_order)). Check shared-anchor
    #    first — it's the scope-doc clause — then fall back to cell
    #    adjacency for the degenerate case where the anchor identity
    #    is absent (shouldn't happen in Linked-CP assembly but kept
    #    as a belt-and-braces check).
    for idx in range(len(chosen_corridors) - 1):
        this_c = chosen_corridors[idx]
        next_c = chosen_corridors[idx + 1]
        if not this_c.path_cells or not next_c.path_cells:
            return AssemblyError(
                reason="invalid_schema",
                detail=(
                    f"interval {idx} or {idx + 1} has empty path_cells"
                ),
                interval_index=idx,
            )
        shares_anchor = (
            this_c.dst.tag == next_c.src.tag
            and this_c.dst.order == next_c.src.order
        )
        if shares_anchor:
            continue
        end_cell = this_c.path_cells[-1]
        start_cell = next_c.path_cells[0]
        if not _cells_continuous(end_cell, start_cell):
            return AssemblyError(
                reason="chain_broken",
                detail=(
                    f"interval {idx} ends at {end_cell} but interval "
                    f"{idx + 1} starts at {start_cell}; Chebyshev "
                    "distance > 1 and no shared anchor block"
                ),
                interval_index=idx,
            )

    # 4. Aggregates.
    cells_total = sum(c.path_length for c in chosen_corridors)
    estimated_time_ms = sum(c.expected_time_ms for c in chosen_corridors)
    ai_confidence = (
        sum(c.learned_corridor_score for c in chosen_corridors)
        / len(chosen_corridors)
    )

    return AssembledRoute(
        map_id=inputs.map_id,
        anchors=inputs.anchors,
        intervals=tuple(intervals),
        cells_total=cells_total,
        estimated_time_ms=estimated_time_ms,
        ai_confidence=float(ai_confidence),
    )


# ---------------------------------------------------------------------
# DB wrapper
# ---------------------------------------------------------------------

_ANCHOR_QUERY = """
SELECT waypoint_index, waypoint_order, tag, x, y, z
FROM map_checkpoints
WHERE map_id = %s
  AND tag IN ('Spawn', 'Checkpoint', 'LinkedCheckpoint', 'Goal')
ORDER BY
    CASE tag
        WHEN 'Spawn' THEN 0
        WHEN 'Checkpoint' THEN 1
        WHEN 'LinkedCheckpoint' THEN 1
        WHEN 'Goal' THEN 2
    END,
    waypoint_order
"""

_CORRIDORS_QUERY = """
SELECT id, map_id, src_tag, src_order, dst_tag, dst_order,
       path_cells, path_length, contains_virtual_edge,
       corridor_confidence, learned_corridor_score,
       combined_sequence_score
FROM route_corridors
WHERE map_id = %s
  AND classification_version = %s
"""


def _parse_cells(raw: str) -> tuple[Cell, ...]:
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return ()
    out: list[Cell] = []
    for c in data:
        if isinstance(c, (list, tuple)) and len(c) == 3:
            try:
                out.append((int(c[0]), int(c[1]), int(c[2])))
            except (TypeError, ValueError):
                continue
    return tuple(out)


def _detect_and_order_anchors(
    rows: Sequence[tuple],
) -> tuple[bool, tuple[Anchor, ...]]:
    """Turn the map_checkpoints rows into (is_linked_cp, ordered anchors).

    Linked-CP detection: the map uses the dedicated ``LinkedCheckpoint``
    tag exclusively (no mixed shapes). This matches the parser's own
    discriminator and the enumeration-time shape in
    :func:`src.corridor.traversability.enumeration._plan_intervals`;
    both sides of the assembler/enumerator contract must agree on this
    rule or interval keys won't line up.

    Anchor ordering: Spawn first, then Checkpoints in ascending
    ``waypoint_order``, then Goal. Ties on ``waypoint_order`` are
    resolved by ``waypoint_index`` (DB-returned secondary sort)."""
    # A single logical checkpoint can span multiple cells (multi-cell
    # gate — the parser emits one row per cell with shared tag+order).
    # The enumerator collapses these via AnchorSet keyed on
    # (tag, waypoint_order); the assembler must do the same or the
    # chain will contain duplicate (tag, order) anchors and it'll look
    # for a self-interval "LCP#1 → LCP#1" that doesn't exist.
    spawn: Anchor | None = None
    goal: Anchor | None = None
    plain_cps: dict[int, Anchor] = {}
    linked_cps: dict[int, Anchor] = {}
    for r in rows:
        _idx, order, tag, x, y, z = r
        cell = (int(x), int(y), int(z)) if (
            x is not None and y is not None and z is not None
        ) else None
        anchor = Anchor(tag=str(tag), order=int(order), cell=cell)
        if tag == "Spawn":
            # First Spawn wins; a multi-cell Spawn is rare but if it
            # happens any of its cells serves as the representative.
            if spawn is None:
                spawn = anchor
        elif tag == "Goal":
            if goal is None:
                goal = anchor
        elif tag == "Checkpoint":
            plain_cps.setdefault(int(order), anchor)
        elif tag == "LinkedCheckpoint":
            linked_cps.setdefault(int(order), anchor)
        # Other tags are ignored — route ends at Goal.
    linked = bool(linked_cps) and not plain_cps and goal is not None
    ordered: list[Anchor] = []
    if spawn is not None:
        ordered.append(spawn)
    cps_source = linked_cps if linked else {**plain_cps, **linked_cps}
    ordered.extend(cps_source[k] for k in sorted(cps_source))
    if goal is not None:
        ordered.append(goal)
    return linked, tuple(ordered)


def assemble_route(
    conn: Connection,
    map_id: int,
    *,
    classification_version: str = CLASSIFICATION_VERSION,
    random_seed: int = 0,
    top_k_candidates: int = 1,
    validator: (
        "Callable[[CandidateCorridor, int], float | None] | None"
    ) = None,
) -> AssembledRoute | AssemblyError:
    """DB-facing wrapper. Fetches anchors + candidate corridors, then
    delegates to :func:`assemble_route_from_inputs`.

    ``random_seed`` + ``top_k_candidates`` enable Level-1 mutation; see
    :class:`AssemblyInputs`.

    ``validator``, if provided, is a callable invoked once per
    candidate within the top-K pool of each interval —
    ``(candidate, interval_index) -> validation_score in [0, 1] | None``.
    Scored candidates carry the score into the extended tie-break
    (see :func:`_tie_break_key_with_validation`). Running the
    validator on every enumerated corridor would be wasteful — only
    the top few per interval can possibly win on learned score —
    so the wrapper limits the validation to the top ``max(top_k_candidates,
    _VALIDATOR_CANDIDATE_CAP)`` per interval after a preliminary
    learned-score sort.
    """
    with cursor(conn) as cur:
        cur.execute(_ANCHOR_QUERY, (map_id,))
        anchor_rows = cur.fetchall()
        cur.execute(_CORRIDORS_QUERY, (map_id, classification_version))
        corridor_rows = cur.fetchall()

    linked, anchors = _detect_and_order_anchors(anchor_rows)
    candidates: list[CandidateCorridor] = []
    for r in corridor_rows:
        (
            cid, mid, src_tag, src_order, dst_tag, dst_order,
            path_cells_raw, path_length, virtual,
            conf, learned, seq_score,
        ) = r
        cells = _parse_cells(path_cells_raw)
        candidates.append(CandidateCorridor(
            corridor_id=int(cid),
            map_id=int(mid),
            src=Anchor(tag=str(src_tag), order=int(src_order)),
            dst=Anchor(tag=str(dst_tag), order=int(dst_order)),
            path_cells=cells,
            path_length=int(path_length),
            contains_virtual_edge=bool(virtual),
            corridor_confidence=(
                float(conf) if conf is not None else None
            ),
            learned_corridor_score=(
                float(learned) if learned is not None else None
            ),
            combined_sequence_score=(
                float(seq_score) if seq_score is not None else None
            ),
        ))

    # Validator pass — score the top few candidates per interval
    # on the learned-score tie-break. Running validator on every
    # enumerated corridor costs too much; the top-K that can
    # possibly win learned is what matters.
    if validator is not None:
        candidates = _score_top_candidates_per_interval(
            candidates=candidates,
            validator=validator,
            per_interval_cap=max(top_k_candidates, _VALIDATOR_CANDIDATE_CAP),
        )

    return assemble_route_from_inputs(AssemblyInputs(
        map_id=map_id,
        is_linked_cp=linked,
        anchors=anchors,
        candidates=tuple(candidates),
        random_seed=random_seed,
        top_k_candidates=top_k_candidates,
        use_validation_tie_break=validator is not None,
    ))


# How many top-learned-score candidates per interval to validate.
# The top-K mutation pick samples from the first K tie-break entries;
# beyond that, learned-score differences make them practically unable
# to win. 10 leaves headroom over the default top_k_candidates=3.
_VALIDATOR_CANDIDATE_CAP: int = 10


def _score_top_candidates_per_interval(
    *,
    candidates: list[CandidateCorridor],
    validator: "Callable[[CandidateCorridor, int], float | None]",
    per_interval_cap: int,
) -> list[CandidateCorridor]:
    """For each (src, dst) interval, take the top ``per_interval_cap``
    candidates by ``_tie_break_key`` and ask the validator for a
    score. Leaves the rest of the candidate list untouched so the
    assembly pool itself stays complete — only the top few carry a
    validation_score.
    """
    by_interval: dict[
        tuple[str, int, str, int], list[CandidateCorridor]
    ] = {}
    for c in candidates:
        by_interval.setdefault(
            (c.src.tag, c.src.order, c.dst.tag, c.dst.order), [],
        ).append(c)

    scored_ids: set[int] = set()
    replacements: dict[int, CandidateCorridor] = {}

    # Anchor ordering → interval_index. Build on the fly from the
    # sorted list of (src_order, src_tag) pairs — matches the order
    # the main assembly loop iterates.
    interval_order = sorted(
        by_interval.keys(),
        key=lambda k: (k[1], k[3], k[0], k[2]),
    )
    for idx, key in enumerate(interval_order):
        pool = list(by_interval[key])
        pool.sort(key=_tie_break_key)
        for c in pool[:per_interval_cap]:
            if c.learned_corridor_score is None:
                continue
            score = validator(c, idx)
            if score is None:
                continue
            replacements[id(c)] = CandidateCorridor(
                corridor_id=c.corridor_id, map_id=c.map_id,
                src=c.src, dst=c.dst,
                path_cells=c.path_cells, path_length=c.path_length,
                contains_virtual_edge=c.contains_virtual_edge,
                corridor_confidence=c.corridor_confidence,
                learned_corridor_score=c.learned_corridor_score,
                combined_sequence_score=c.combined_sequence_score,
                validation_score=float(score),
            )
            scored_ids.add(c.corridor_id)

    if not replacements:
        return candidates

    out: list[CandidateCorridor] = []
    for c in candidates:
        out.append(replacements.get(id(c), c))
    _LOG.info(
        "assembly validator: scored %d candidate(s) across %d interval(s)",
        len(scored_ids), len(interval_order),
    )
    return out
