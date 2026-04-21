"""Per-map cohort assignment.

Given a map's set of clean (and usable-with-warnings) finished
replays, assign each to zero-or-more cohorts based on its
finish-time percentile rank. A replay may be in several cohorts.

Cohort semantics (from ``CLAUDE.md``):

- **intent**      — broad median-player runs; used for route inference
- **performance** — stronger / top runs
- **robustness**  — wider distribution
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.schema.replays import ReplayCohort


@dataclass(frozen=True)
class CohortAssignmentConfig:
    intent_lower_pct: float = 0.20
    intent_upper_pct: float = 0.80
    performance_top_pct: float = 0.10
    robustness_lower_pct: float = 0.05
    robustness_upper_pct: float = 0.95
    small_map_n: int = 3  # below this, every finished replay gets every cohort

    def __post_init__(self) -> None:
        for name, lo, hi in (
            ("intent", self.intent_lower_pct, self.intent_upper_pct),
            ("robustness", self.robustness_lower_pct, self.robustness_upper_pct),
        ):
            if not 0.0 <= lo <= hi <= 1.0:
                raise ValueError(f"{name} percentile band invalid: [{lo}, {hi}]")
        if not 0.0 <= self.performance_top_pct <= 1.0:
            raise ValueError("performance_top_pct out of [0,1]")
        if self.small_map_n < 1:
            raise ValueError("small_map_n must be >= 1")


@dataclass(frozen=True)
class CohortAssignment:
    replay_id: int
    cohorts: frozenset[ReplayCohort]


_ALL_COHORTS = frozenset(ReplayCohort)


def assign_cohorts_for_map(
    finished_clean_replays: Sequence[tuple[int, int]],
    *,
    config: CohortAssignmentConfig | None = None,
) -> list[CohortAssignment]:
    """Assign cohorts to each replay. Input is ``[(replay_id, finish_time_ms), ...]``.

    Only finished replays with a ``finish_time_ms`` should be passed in;
    the caller is responsible for filtering out rejected replays. The
    function is pure — no DB access.
    """
    cfg = config or CohortAssignmentConfig()
    n = len(finished_clean_replays)
    if n == 0:
        return []
    if n < cfg.small_map_n:
        return [CohortAssignment(rid, _ALL_COHORTS) for rid, _ in finished_clean_replays]

    ordered = sorted(finished_clean_replays, key=lambda row: row[1])
    out: list[CohortAssignment] = []
    for i, (rid, _) in enumerate(ordered):
        pct = i / (n - 1)
        cohorts: set[ReplayCohort] = set()
        if pct <= cfg.performance_top_pct:
            cohorts.add(ReplayCohort.PERFORMANCE)
        if cfg.intent_lower_pct <= pct <= cfg.intent_upper_pct:
            cohorts.add(ReplayCohort.INTENT)
        if cfg.robustness_lower_pct <= pct <= cfg.robustness_upper_pct:
            cohorts.add(ReplayCohort.ROBUSTNESS)
        out.append(CohortAssignment(rid, frozenset(cohorts)))
    return out


@dataclass(frozen=True)
class MapCohortStats:
    map_id: int
    total_finished: int
    performance: int
    intent: int
    robustness: int


def summarize(
    map_id: int,
    assignments: Sequence[CohortAssignment],
) -> MapCohortStats:
    p = i = r = 0
    for a in assignments:
        if ReplayCohort.PERFORMANCE in a.cohorts:
            p += 1
        if ReplayCohort.INTENT in a.cohorts:
            i += 1
        if ReplayCohort.ROBUSTNESS in a.cohorts:
            r += 1
    return MapCohortStats(
        map_id=map_id,
        total_finished=len(assignments),
        performance=p,
        intent=i,
        robustness=r,
    )
