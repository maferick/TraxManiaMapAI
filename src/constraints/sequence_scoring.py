"""Phase 2 #218-4 — pair-sequence scoring.

Combines ``block_pair_transitions`` frequency counts with
``block_geometry`` shape-compatibility heuristics into a single
``SequenceScore`` per ordered (A → B) block pair.

**Hard boundary** (scope #218): purely a soft signal. Callers that
make safety decisions — generation's finishability gate, the strip
policy's "is this block drivable" check — must consult
:mod:`src.corridor.traversability.classification` separately. This
module exposes the signals as numbers with context; the guardrail
against frequency overriding traversability lives in the caller.

Three numbers per pair:

- ``pattern_score`` ∈ [0, 1] — transition frequency normalised
  against the pair's source-block marginal. 0.9 means "this B
  follows this A in 90% of the A-driven-through observations."
- ``geometry_score`` ∈ [0, 1] — shape-compatibility heuristic from
  :mod:`src.constraints.block_geometry`'s shape_class + surface_hint
  + is_deco / is_anchor_capable fields.
- ``combined_score`` ∈ [0, 1] — weighted combination
  (α=0.55 pattern, β=0.45 geometry by default). Exposed for
  generation ranking; callers pick their own thresholds.

Plus a ``pattern_rarity`` bucket (``common`` / ``uncommon`` /
``rare`` / ``unseen``) so callers can log rare-but-valid transitions
as warnings without having to interpret the raw float.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pymysql.connections import Connection

from src.constraints.block_geometry import (
    BlockGeometry,
    SHAPE_CHECKPOINT,
    SHAPE_CURVE,
    SHAPE_DECO,
    SHAPE_FINISH,
    SHAPE_GATE,
    SHAPE_LOOP,
    SHAPE_PLATFORM,
    SHAPE_RAMP,
    SHAPE_START,
    SHAPE_STRAIGHT,
    SHAPE_SUPPORT,
    SHAPE_UNKNOWN,
    fetch_geometry,
)
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


# Default weighting in combined_score. Slight pattern bias because
# pattern_score reflects what the corpus already drives through
# (evidence-of-playability) whereas geometry_score is heuristic.
# Override via `score_pair(..., alpha=...)` for A/B tests.
DEFAULT_PATTERN_WEIGHT: float = 0.55
DEFAULT_GEOMETRY_WEIGHT: float = 0.45

# Rarity buckets, cumulative from the top.
_RARITY_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (0.20, "common"),     # ≥20% of marginal — "standard follow-up"
    (0.05, "uncommon"),   #  5-20% — "seen but not default"
    (0.00, "rare"),       #  <5%, but non-zero — "valid, atypical"
)


# Shape-adjacency matrix: pairs of (shape_a, shape_b) that fit
# together well. Ordered — "ramp → straight" feels natural (you land
# from a ramp onto flat road), "straight → ramp" also. Symmetric here
# because shape-level compatibility doesn't encode direction;
# directionality lives in the pattern counts, which do.
_SHAPE_COMPATIBLE_PAIRS: frozenset[tuple[str, str]] = frozenset((
    # Straight continuations of the same surface
    (SHAPE_STRAIGHT, SHAPE_STRAIGHT),
    (SHAPE_CURVE, SHAPE_STRAIGHT),
    (SHAPE_STRAIGHT, SHAPE_CURVE),
    (SHAPE_CURVE, SHAPE_CURVE),
    (SHAPE_PLATFORM, SHAPE_PLATFORM),
    # Ramps join straight / curve / platform
    (SHAPE_RAMP, SHAPE_STRAIGHT), (SHAPE_STRAIGHT, SHAPE_RAMP),
    (SHAPE_RAMP, SHAPE_CURVE), (SHAPE_CURVE, SHAPE_RAMP),
    (SHAPE_RAMP, SHAPE_PLATFORM), (SHAPE_PLATFORM, SHAPE_RAMP),
    (SHAPE_RAMP, SHAPE_RAMP),
    # Loops enter and exit through straights or ramps
    (SHAPE_STRAIGHT, SHAPE_LOOP), (SHAPE_LOOP, SHAPE_STRAIGHT),
    (SHAPE_RAMP, SHAPE_LOOP), (SHAPE_LOOP, SHAPE_RAMP),
    (SHAPE_LOOP, SHAPE_LOOP),
    # Anchors connect to structural shapes
    (SHAPE_START, SHAPE_STRAIGHT), (SHAPE_START, SHAPE_RAMP),
    (SHAPE_START, SHAPE_CURVE), (SHAPE_START, SHAPE_PLATFORM),
    (SHAPE_CHECKPOINT, SHAPE_STRAIGHT), (SHAPE_STRAIGHT, SHAPE_CHECKPOINT),
    (SHAPE_CHECKPOINT, SHAPE_RAMP), (SHAPE_RAMP, SHAPE_CHECKPOINT),
    (SHAPE_CHECKPOINT, SHAPE_CURVE), (SHAPE_CURVE, SHAPE_CHECKPOINT),
    (SHAPE_CHECKPOINT, SHAPE_PLATFORM), (SHAPE_PLATFORM, SHAPE_CHECKPOINT),
    (SHAPE_CHECKPOINT, SHAPE_CHECKPOINT),
    (SHAPE_FINISH, SHAPE_STRAIGHT), (SHAPE_STRAIGHT, SHAPE_FINISH),
    (SHAPE_FINISH, SHAPE_RAMP), (SHAPE_RAMP, SHAPE_FINISH),
    # Gates are traversable; couple like anchors with the neighbours
    (SHAPE_GATE, SHAPE_STRAIGHT), (SHAPE_STRAIGHT, SHAPE_GATE),
    (SHAPE_GATE, SHAPE_RAMP), (SHAPE_RAMP, SHAPE_GATE),
    (SHAPE_GATE, SHAPE_CHECKPOINT), (SHAPE_CHECKPOINT, SHAPE_GATE),
))


@dataclass(frozen=True)
class SequenceScore:
    """Per-pair scoring result. All three scores ∈ [0, 1]; higher =
    "better evidence / more plausible connection." The caller chooses
    thresholds — there's no magic cutoff."""
    pattern_score: float
    geometry_score: float
    combined_score: float
    pattern_rarity: str      # common | uncommon | rare | unseen
    transition_count: int
    marginal_total: int
    geometry_detail: str     # short explainer for debug logs
    reasoning: str           # single-line "why this score" for operators


# ---------------------------------------------------------------------
# Pattern score
# ---------------------------------------------------------------------

_PATTERN_LOOKUP_SQL = """
SELECT transition_count
FROM block_pair_transitions
WHERE block_family_a = %s AND block_name_a = %s
  AND block_family_b = %s AND block_name_b = %s
  AND environment = %s
"""

_MARGINAL_SQL = """
SELECT COALESCE(SUM(transition_count), 0)
FROM block_pair_transitions
WHERE block_family_a = %s AND block_name_a = %s
  AND environment = %s
"""


def _fetch_pattern_score(
    conn: Connection,
    a_family: str, a_name: str,
    b_family: str, b_name: str,
    environment: str,
) -> tuple[float, int, int]:
    """Return (pattern_score, transition_count, marginal_total).

    Marginal = total transitions seen leaving block A in this
    environment. Normalising by the marginal answers "given we're
    at A, how often do we go to B?" — which is what a generation
    ranker wants. Unseen A or unseen (A, B): score 0."""
    with cursor(conn) as cur:
        cur.execute(
            _PATTERN_LOOKUP_SQL,
            (a_family, a_name, b_family, b_name, environment),
        )
        row = cur.fetchone()
        transition_count = int(row[0]) if row else 0

        cur.execute(_MARGINAL_SQL, (a_family, a_name, environment))
        row = cur.fetchone()
        marginal_total = int(row[0]) if row else 0

    if marginal_total <= 0:
        return 0.0, transition_count, marginal_total
    return transition_count / marginal_total, transition_count, marginal_total


def _rarity_bucket(pattern_score: float, transition_count: int) -> str:
    if transition_count == 0:
        return "unseen"
    for threshold, label in _RARITY_THRESHOLDS:
        if pattern_score >= threshold:
            return label
    return "rare"


# ---------------------------------------------------------------------
# Geometry score
# ---------------------------------------------------------------------

def _geometry_score_for(
    a: BlockGeometry | None, b: BlockGeometry | None,
) -> tuple[float, str]:
    """Return (score ∈ [0, 1], short detail string).

    Baseline 0.4 for "we know both blocks and neither is suspicious";
    bonuses for same surface, recognised adjacent shape pairs,
    unknown-but-plausible fallbacks. Deco / unknown → low."""
    if a is None and b is None:
        return 0.0, "both-blocks-uncatalogued"
    if a is None or b is None:
        # Partial info is worse than nothing — don't guess direction.
        return 0.15, "one-block-uncatalogued"

    score = 0.4
    reasons: list[str] = []

    # Same surface → +0.25. A dirt ramp connecting to a dirt straight
    # is more plausible than a dirt ramp connecting to an ice curve.
    if a.surface_hint and b.surface_hint and a.surface_hint == b.surface_hint:
        score += 0.25
        reasons.append(f"surface={a.surface_hint}")

    # Recognised shape-pair → +0.30. Anchor pairs all count.
    pair = (a.shape_class, b.shape_class)
    if pair in _SHAPE_COMPATIBLE_PAIRS:
        score += 0.30
        reasons.append(f"shape_pair={a.shape_class}→{b.shape_class}")

    # Either deco → heavy penalty. Deco isn't generation-relevant.
    if a.is_deco or b.is_deco:
        score -= 0.35
        reasons.append("has-deco")

    # Either unknown-shape → small penalty. We don't know if it fits.
    if a.shape_class == SHAPE_UNKNOWN or b.shape_class == SHAPE_UNKNOWN:
        score -= 0.10
        reasons.append("has-unknown-shape")

    # Clamp to [0, 1].
    score = max(0.0, min(1.0, score))
    detail = ";".join(reasons) if reasons else "baseline"
    return score, detail


# ---------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------

def _combine(
    pattern: float, geometry: float,
    *, alpha: float, beta: float,
) -> float:
    total_weight = alpha + beta
    if total_weight <= 0:
        return 0.0
    return (alpha * pattern + beta * geometry) / total_weight


def score_pair(
    conn: Connection,
    *,
    a_family: str, a_name: str,
    b_family: str, b_name: str,
    environment: str,
    alpha: float = DEFAULT_PATTERN_WEIGHT,
    beta: float = DEFAULT_GEOMETRY_WEIGHT,
) -> SequenceScore:
    """Score one ordered (A → B) block pair. Raises nothing on
    unseen input — unseen pairs get pattern_score=0 + rarity=unseen
    and a geometry_score that reflects the shape knowledge only.

    IMPORTANT: caller is responsible for the traversability-evidence
    guardrail. Even a pair with combined_score=1.0 MUST be rejected
    if the map's classification graph says the edge is
    non-drivable. See scope #218 ("Do not let frequency override
    traversability")."""
    pattern_score, transition_count, marginal_total = _fetch_pattern_score(
        conn, a_family, a_name, b_family, b_name, environment,
    )
    a_geom = fetch_geometry(conn, a_family, a_name)
    b_geom = fetch_geometry(conn, b_family, b_name)
    geometry_score, geometry_detail = _geometry_score_for(a_geom, b_geom)

    combined = _combine(
        pattern_score, geometry_score, alpha=alpha, beta=beta,
    )
    rarity = _rarity_bucket(pattern_score, transition_count)

    reasoning = (
        f"pattern={pattern_score:.3f} "
        f"({transition_count}/{marginal_total} [{rarity}]) · "
        f"geometry={geometry_score:.3f} ({geometry_detail})"
    )

    return SequenceScore(
        pattern_score=pattern_score,
        geometry_score=geometry_score,
        combined_score=combined,
        pattern_rarity=rarity,
        transition_count=transition_count,
        marginal_total=marginal_total,
        geometry_detail=geometry_detail,
        reasoning=reasoning,
    )
