"""Expected-value-to-learning scoring for replay backfill candidates.

Per-map score answering *"how much does one more replay help the
learned-ranking signal?"* — computed from artifacts we already have
(corridor count, clean-replay count, cohort-threshold proximity).
No popularity, no award counts, no rank-derived inputs.

See docs/learning/replay-coverage-expansion-plan.md for the design
and non-goals.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from pymysql.connections import Connection

from src.storage.mariadb import cursor


# TMX's legacy /api/replays/get_replays/{mapId} endpoint returns
# at most 25 replays regardless of the `amount` parameter. A map
# at this cap has no remaining headroom; more ingest attempts are
# waste. Update here if TMX exposes a higher cap later.
SATURATION_PER_MAP: int = 25


@dataclass(frozen=True)
class CohortThresholdConfig:
    """Cohort percentile thresholds pulled from config. Used for
    per-map "one replay from flipping the bucket" proximity check.
    Values are percentages in [0, 100] (or fractions in [0, 1]) —
    normalized at construction time."""
    intent_lower_pct: float
    intent_upper_pct: float
    performance_top_pct: float
    robustness_lower_pct: float
    robustness_upper_pct: float

    @classmethod
    def from_config(cls, cfg: dict) -> "CohortThresholdConfig":
        """Accept either fractional (0.20) or percentage (20.0) forms."""
        section = cfg.get("cohorts") or {}
        def _pct(v: float | int | None, default: float) -> float:
            if v is None:
                return default
            v = float(v)
            return v * 100.0 if v <= 1.0 else v
        return cls(
            intent_lower_pct=_pct(section.get("intent_lower_pct"), 20.0),
            intent_upper_pct=_pct(section.get("intent_upper_pct"), 80.0),
            performance_top_pct=_pct(section.get("performance_top_pct"), 10.0),
            robustness_lower_pct=_pct(section.get("robustness_lower_pct"), 5.0),
            robustness_upper_pct=_pct(section.get("robustness_upper_pct"), 95.0),
        )

    def boundaries(self) -> tuple[float, ...]:
        """Sorted unique percentile boundaries that a map could cross
        via additional replays. Used by ``near_cohort_boundary``."""
        return tuple(sorted({
            self.intent_lower_pct,
            self.intent_upper_pct,
            self.performance_top_pct,
            self.robustness_lower_pct,
            self.robustness_upper_pct,
        }))


@dataclass(frozen=True)
class MapCoverage:
    """Per-map snapshot: what we currently have for this map."""
    map_id: int
    source_map_id: str | None
    title: str | None
    corridor_count: int
    total_replays: int                 # any clean_status
    clean_replays: int                 # clean or usable_with_warnings
    percentile_rank_clean: float       # this map's rank (1-100) by clean_replays
                                       # among all maps; 100 = most replays
    value_score: float = 0.0
    saturated: bool = False
    near_cohort_boundary: bool = False


@dataclass(frozen=True)
class RecommendedBackfill:
    """One row in the top-N recommendation list."""
    map_id: int
    source_map_id: str | None
    title: str | None
    corridor_count: int
    clean_replays: int
    value_score: float
    reason: str                        # short human-readable tag


@dataclass
class CoverageReport:
    """Bundle the CLI + renderer consume. All counts are derived;
    the interesting fields are the buckets and the backfill list."""
    total_maps: int
    corridor_owning_maps: int
    saturated_maps: list[MapCoverage] = field(default_factory=list)
    zero_replay_corridor_maps: list[MapCoverage] = field(default_factory=list)
    near_cohort_boundary_maps: list[MapCoverage] = field(default_factory=list)
    backfill_recommendation: list[RecommendedBackfill] = field(default_factory=list)
    all_maps: list[MapCoverage] = field(default_factory=list)


# ---------------------------------------------------------------------
# Scoring primitives
# ---------------------------------------------------------------------

def marginal_gain(clean_replays: int) -> float:
    """Expected fractional improvement from adding one more replay.

    - At saturation → 0.0 (can't pull more anyway).
    - At 0 replays → 1.0 (the "first clean replay" unlocks an entire
      map's labels).
    - Otherwise → 1 / sqrt(n + 1), smoothly diminishing.

    Matches the formula documented in the expansion plan.
    """
    if clean_replays < 0:
        return 0.0
    if clean_replays >= SATURATION_PER_MAP:
        return 0.0
    if clean_replays == 0:
        return 1.0
    return 1.0 / math.sqrt(clean_replays + 1)


def _near_cohort_boundary(
    percentile_rank: float, boundaries: Sequence[float], tolerance: float = 2.0
) -> bool:
    """Is this map's rank within `tolerance` percentile points of any
    cohort threshold? One more replay could flip its bucket."""
    return any(abs(percentile_rank - b) <= tolerance for b in boundaries)


def score_map(
    *,
    corridor_count: int,
    clean_replays: int,
    percentile_rank_clean: float,
    cohort_boundaries: Sequence[float],
) -> tuple[float, bool]:
    """Return (value_score, near_cohort_boundary). Maps with zero
    corridors score 0 by construction — no learning signal to improve."""
    if corridor_count <= 0:
        return 0.0, False
    gain = marginal_gain(clean_replays)
    if gain == 0.0:
        return 0.0, False
    corridor_weight = math.log(1 + corridor_count)
    near = _near_cohort_boundary(percentile_rank_clean, cohort_boundaries)
    cohort_bonus = 0.5 if near else 0.0
    score = corridor_weight * gain * (1.0 + cohort_bonus)
    return score, near


# ---------------------------------------------------------------------
# DB collection
# ---------------------------------------------------------------------

_COVERAGE_SQL = """
SELECT
    m.id AS map_id,
    m.source_map_id,
    m.title,
    COALESCE(cc.corridor_count, 0) AS corridor_count,
    COALESCE(rc.replay_count, 0)   AS total_replays,
    COALESCE(rc.clean_count, 0)    AS clean_replays
FROM maps m
LEFT JOIN (
    SELECT map_id, COUNT(*) AS corridor_count
    FROM route_corridors
    GROUP BY map_id
) cc ON cc.map_id = m.id
LEFT JOIN (
    SELECT
        map_id,
        COUNT(*) AS replay_count,
        SUM(CASE WHEN clean_status IN ('clean','usable_with_warnings') THEN 1 ELSE 0 END)
            AS clean_count
    FROM replays
    GROUP BY map_id
) rc ON rc.map_id = m.id
WHERE m.parse_status = 'success'
"""


def _percentile_ranks(values: Sequence[int]) -> list[float]:
    """Inclusive percentile rank of each element in ``values`` against
    the full sample. Ties resolve to the average rank. Output in
    [0, 100] where 100 = largest."""
    n = len(values)
    if n == 0:
        return []
    # Sort indices by value ascending; assign 1-based ranks with
    # average-rank tie handling.
    indexed = sorted(enumerate(values), key=lambda kv: kv[1])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        # 1-indexed ranks for the tied block i..j are i+1..j+1; use mean.
        avg = ((i + 1) + (j + 1)) / 2.0
        for k in range(i, j + 1):
            orig_idx = indexed[k][0]
            ranks[orig_idx] = avg
        i = j + 1
    # Convert to percentile in [0, 100].
    return [100.0 * r / n for r in ranks]


def fetch_coverage(
    conn: Connection,
    *,
    thresholds: CohortThresholdConfig,
    snapshot_id: str | None = None,
) -> list[MapCoverage]:
    """One pass over ``maps`` joined to corridor + replay counts.
    Computes percentile ranks in Python so we don't need window
    functions (MariaDB 10.1 compatibility). Returns one
    :class:`MapCoverage` per parsed map."""
    params: list = []
    sql = _COVERAGE_SQL
    if snapshot_id is not None:
        sql += " AND m.ingestion_snapshot = %s"
        params.append(snapshot_id)
    with cursor(conn) as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()

    clean_counts = [int(r[5]) for r in rows]
    percentiles = _percentile_ranks(clean_counts)

    boundaries = thresholds.boundaries()
    out: list[MapCoverage] = []
    for r, pct in zip(rows, percentiles):
        map_id = int(r[0])
        source_map_id = str(r[1]) if r[1] is not None else None
        title = str(r[2]) if r[2] is not None else None
        corridor_count = int(r[3])
        total_replays = int(r[4])
        clean_replays = int(r[5])
        score, near = score_map(
            corridor_count=corridor_count,
            clean_replays=clean_replays,
            percentile_rank_clean=pct,
            cohort_boundaries=boundaries,
        )
        saturated = clean_replays >= SATURATION_PER_MAP
        out.append(MapCoverage(
            map_id=map_id,
            source_map_id=source_map_id,
            title=title,
            corridor_count=corridor_count,
            total_replays=total_replays,
            clean_replays=clean_replays,
            percentile_rank_clean=pct,
            value_score=score,
            saturated=saturated,
            near_cohort_boundary=near,
        ))
    return out


# ---------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------

def select_backfill(
    maps: Sequence[MapCoverage], *, top_n: int = 200,
) -> list[RecommendedBackfill]:
    """Pick the top-N maps by value_score. Excludes saturated and
    no-corridor maps. Tie-break by corridor_count desc, then map_id asc
    for determinism."""
    candidates = [m for m in maps if m.value_score > 0]
    candidates.sort(
        key=lambda m: (-m.value_score, -m.corridor_count, m.map_id),
    )
    out: list[RecommendedBackfill] = []
    for m in candidates[:top_n]:
        reasons: list[str] = []
        if m.clean_replays == 0:
            reasons.append("zero clean replays")
        if m.near_cohort_boundary:
            reasons.append("near cohort boundary")
        if not reasons:
            reasons.append("marginal gain high")
        out.append(RecommendedBackfill(
            map_id=m.map_id,
            source_map_id=m.source_map_id,
            title=m.title,
            corridor_count=m.corridor_count,
            clean_replays=m.clean_replays,
            value_score=m.value_score,
            reason="; ".join(reasons),
        ))
    return out


def build_report(
    maps: Sequence[MapCoverage], *, top_n: int = 200,
) -> CoverageReport:
    corridor_owning = [m for m in maps if m.corridor_count > 0]
    saturated = [m for m in corridor_owning if m.saturated]
    zero_replay_corridors = [
        m for m in corridor_owning
        if m.clean_replays == 0 and not m.saturated
    ]
    near_boundary = [
        m for m in corridor_owning
        if m.near_cohort_boundary and not m.saturated
    ]
    backfill = select_backfill(maps, top_n=top_n)
    return CoverageReport(
        total_maps=len(maps),
        corridor_owning_maps=len(corridor_owning),
        saturated_maps=saturated,
        zero_replay_corridor_maps=zero_replay_corridors,
        near_cohort_boundary_maps=near_boundary,
        backfill_recommendation=backfill,
        all_maps=list(maps),
    )
