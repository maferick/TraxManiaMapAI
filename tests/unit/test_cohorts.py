from __future__ import annotations

import pytest

from src.replay.cohorts import (
    CohortAssignmentConfig,
    assign_cohorts_for_map,
    summarize,
)
from src.schema.replays import ReplayCohort


def test_empty_returns_empty() -> None:
    assert assign_cohorts_for_map([]) == []


def test_small_map_assigns_all_cohorts() -> None:
    replays = [(1, 30_000), (2, 31_000)]  # n=2 < small_map_n=3
    result = assign_cohorts_for_map(replays)
    assert len(result) == 2
    for a in result:
        assert a.cohorts == frozenset(ReplayCohort)


def test_large_distribution_assigns_by_percentile() -> None:
    # 21 replays, linearly distributed finish times
    replays = [(i, 20_000 + i * 500) for i in range(21)]
    result = assign_cohorts_for_map(replays)
    by_id = {a.replay_id: a for a in result}

    # fastest (replay 0, pct=0): only performance. Robustness deliberately
    # excludes the bottom 5% of finish times — these fastest-of-fast runs
    # are outliers for route-distribution work.
    assert by_id[0].cohorts == frozenset({ReplayCohort.PERFORMANCE})

    # replay 2 (pct=0.1): at the performance top boundary AND inside the
    # robustness band [0.05, 0.95].
    assert ReplayCohort.PERFORMANCE in by_id[2].cohorts
    assert ReplayCohort.ROBUSTNESS in by_id[2].cohorts

    # median (replay 10, pct=0.5): intent + robustness, not performance.
    assert ReplayCohort.INTENT in by_id[10].cohorts
    assert ReplayCohort.ROBUSTNESS in by_id[10].cohorts
    assert ReplayCohort.PERFORMANCE not in by_id[10].cohorts

    # slowest (replay 20, pct=1.0): nothing (above robustness_upper_pct=0.95).
    assert by_id[20].cohorts == frozenset()


def test_custom_config() -> None:
    cfg = CohortAssignmentConfig(
        intent_lower_pct=0.0,
        intent_upper_pct=1.0,
        performance_top_pct=0.0,
        robustness_lower_pct=0.0,
        robustness_upper_pct=1.0,
        small_map_n=1,
    )
    replays = [(1, 30_000), (2, 31_000), (3, 32_000)]
    result = assign_cohorts_for_map(replays, config=cfg)
    # With this config, every replay is in intent + robustness, and only
    # replay 1 (pct=0.0) is in performance (performance_top_pct=0.0).
    by_id = {a.replay_id: a.cohorts for a in result}
    assert ReplayCohort.INTENT in by_id[1]
    assert ReplayCohort.ROBUSTNESS in by_id[1]
    assert ReplayCohort.PERFORMANCE in by_id[1]
    assert ReplayCohort.PERFORMANCE not in by_id[2]
    assert ReplayCohort.PERFORMANCE not in by_id[3]


def test_invalid_bands_rejected() -> None:
    with pytest.raises(ValueError, match="intent"):
        CohortAssignmentConfig(intent_lower_pct=0.9, intent_upper_pct=0.1)


def test_summarize() -> None:
    replays = [(i, 20_000 + i * 500) for i in range(21)]
    assignments = assign_cohorts_for_map(replays)
    stats = summarize(42, assignments)
    assert stats.map_id == 42
    assert stats.total_finished == 21
    assert stats.performance > 0
    assert stats.intent > 0
    assert stats.robustness > 0
