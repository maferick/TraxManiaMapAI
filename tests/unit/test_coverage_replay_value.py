"""Unit tests for the replay-value scoring + selection + report."""
from __future__ import annotations

import math

import pytest

from src.coverage.replay_value import (
    SATURATION_PER_MAP,
    CohortThresholdConfig,
    MapCoverage,
    _near_cohort_boundary,
    _percentile_ranks,
    build_report,
    marginal_gain,
    score_map,
    select_backfill,
)
from src.coverage.report import render_markdown


# ---------------------------------------------------------------------
# marginal_gain
# ---------------------------------------------------------------------

class TestMarginalGain:
    def test_zero_replays_returns_one(self) -> None:
        assert marginal_gain(0) == 1.0

    def test_monotone_decreasing(self) -> None:
        vals = [marginal_gain(n) for n in range(0, SATURATION_PER_MAP)]
        # Allow equality only at boundaries; strict elsewhere.
        for a, b in zip(vals, vals[1:]):
            assert a > b

    def test_saturation_returns_zero(self) -> None:
        assert marginal_gain(SATURATION_PER_MAP) == 0.0
        assert marginal_gain(SATURATION_PER_MAP + 10) == 0.0

    def test_negative_returns_zero(self) -> None:
        assert marginal_gain(-1) == 0.0

    def test_specific_value_at_three(self) -> None:
        # 1 / sqrt(4) = 0.5
        assert marginal_gain(3) == pytest.approx(0.5)


# ---------------------------------------------------------------------
# Cohort threshold handling
# ---------------------------------------------------------------------

class TestCohortThresholds:
    def test_fractional_form_normalized_to_percent(self) -> None:
        cfg = {"cohorts": {
            "intent_lower_pct": 0.20, "intent_upper_pct": 0.80,
            "performance_top_pct": 0.10,
            "robustness_lower_pct": 0.05, "robustness_upper_pct": 0.95,
        }}
        t = CohortThresholdConfig.from_config(cfg)
        assert t.intent_lower_pct == 20.0
        assert t.performance_top_pct == 10.0

    def test_percent_form_preserved(self) -> None:
        cfg = {"cohorts": {
            "intent_lower_pct": 20, "intent_upper_pct": 80,
            "performance_top_pct": 10,
            "robustness_lower_pct": 5, "robustness_upper_pct": 95,
        }}
        t = CohortThresholdConfig.from_config(cfg)
        assert t.intent_lower_pct == 20.0

    def test_defaults_when_missing(self) -> None:
        t = CohortThresholdConfig.from_config({})
        assert t.intent_lower_pct == 20.0
        assert t.intent_upper_pct == 80.0

    def test_boundaries_sorted_unique(self) -> None:
        t = CohortThresholdConfig(10.0, 50.0, 10.0, 5.0, 95.0)
        assert t.boundaries() == (5.0, 10.0, 50.0, 95.0)

    def test_near_boundary_within_tolerance(self) -> None:
        assert _near_cohort_boundary(19.0, [20.0, 80.0]) is True
        assert _near_cohort_boundary(80.5, [20.0, 80.0]) is True

    def test_near_boundary_outside_tolerance(self) -> None:
        assert _near_cohort_boundary(50.0, [20.0, 80.0]) is False


# ---------------------------------------------------------------------
# score_map
# ---------------------------------------------------------------------

class TestScoreMap:
    def test_no_corridors_scores_zero(self) -> None:
        score, near = score_map(
            corridor_count=0, clean_replays=0,
            percentile_rank_clean=50.0, cohort_boundaries=[20.0, 80.0],
        )
        assert score == 0.0
        assert near is False

    def test_saturated_map_scores_zero(self) -> None:
        score, _ = score_map(
            corridor_count=5, clean_replays=SATURATION_PER_MAP,
            percentile_rank_clean=50.0, cohort_boundaries=[20.0, 80.0],
        )
        assert score == 0.0

    def test_zero_replay_map_highest(self) -> None:
        zero, _ = score_map(
            corridor_count=5, clean_replays=0,
            percentile_rank_clean=50.0, cohort_boundaries=[20.0, 80.0],
        )
        few, _ = score_map(
            corridor_count=5, clean_replays=5,
            percentile_rank_clean=50.0, cohort_boundaries=[20.0, 80.0],
        )
        assert zero > few

    def test_cohort_boundary_multiplies(self) -> None:
        near, near_flag = score_map(
            corridor_count=5, clean_replays=3,
            percentile_rank_clean=20.0, cohort_boundaries=[20.0, 80.0],
        )
        far, far_flag = score_map(
            corridor_count=5, clean_replays=3,
            percentile_rank_clean=50.0, cohort_boundaries=[20.0, 80.0],
        )
        assert near_flag is True
        assert far_flag is False
        assert near > far
        assert near == pytest.approx(far * 1.5)

    def test_corridor_count_log_scaled(self) -> None:
        # More corridors → higher score, but sub-linearly.
        s_small, _ = score_map(
            corridor_count=2, clean_replays=1,
            percentile_rank_clean=50.0, cohort_boundaries=[],
        )
        s_large, _ = score_map(
            corridor_count=20, clean_replays=1,
            percentile_rank_clean=50.0, cohort_boundaries=[],
        )
        assert s_large > s_small
        # log(21)/log(3) ≈ 2.77 — so the gap shouldn't be linear (10x).
        assert s_large / s_small == pytest.approx(
            math.log(21) / math.log(3), rel=0.01,
        )


# ---------------------------------------------------------------------
# Percentile ranks
# ---------------------------------------------------------------------

class TestPercentileRanks:
    def test_monotone_for_distinct_values(self) -> None:
        ranks = _percentile_ranks([10, 20, 30, 40, 50])
        assert ranks == pytest.approx([20.0, 40.0, 60.0, 80.0, 100.0])

    def test_ties_get_average_rank(self) -> None:
        ranks = _percentile_ranks([5, 5, 10])
        # Both 5s share ranks 1 and 2 → avg 1.5; 10 is rank 3.
        assert ranks == pytest.approx([50.0, 50.0, 100.0])

    def test_empty(self) -> None:
        assert _percentile_ranks([]) == []


# ---------------------------------------------------------------------
# select_backfill
# ---------------------------------------------------------------------

def _mk_map(
    *, map_id: int, corridors: int, clean: int, score: float, near: bool = False,
) -> MapCoverage:
    return MapCoverage(
        map_id=map_id,
        source_map_id=f"src{map_id}",
        title=f"Map {map_id}",
        corridor_count=corridors,
        total_replays=clean,
        clean_replays=clean,
        percentile_rank_clean=50.0,
        value_score=score,
        saturated=clean >= SATURATION_PER_MAP,
        near_cohort_boundary=near,
    )


class TestSelectBackfill:
    def test_orders_by_value_desc(self) -> None:
        maps = [
            _mk_map(map_id=1, corridors=3, clean=3, score=1.0),
            _mk_map(map_id=2, corridors=3, clean=0, score=3.0),
            _mk_map(map_id=3, corridors=3, clean=1, score=2.0),
        ]
        out = select_backfill(maps, top_n=10)
        assert [r.map_id for r in out] == [2, 3, 1]

    def test_excludes_zero_score(self) -> None:
        maps = [
            _mk_map(map_id=1, corridors=0, clean=0, score=0.0),
            _mk_map(map_id=2, corridors=3, clean=5, score=1.0),
        ]
        out = select_backfill(maps, top_n=10)
        assert [r.map_id for r in out] == [2]

    def test_top_n_caps_output(self) -> None:
        maps = [_mk_map(map_id=i, corridors=3, clean=i, score=10.0 - i) for i in range(5)]
        out = select_backfill(maps, top_n=2)
        assert len(out) == 2

    def test_zero_replay_labelled(self) -> None:
        out = select_backfill(
            [_mk_map(map_id=1, corridors=3, clean=0, score=1.0)], top_n=10,
        )
        assert "zero clean replays" in out[0].reason

    def test_cohort_boundary_labelled(self) -> None:
        out = select_backfill(
            [_mk_map(map_id=1, corridors=3, clean=5, score=1.0, near=True)],
            top_n=10,
        )
        assert "near cohort boundary" in out[0].reason


# ---------------------------------------------------------------------
# build_report + render
# ---------------------------------------------------------------------

class TestBuildReport:
    def test_buckets_populated_correctly(self) -> None:
        maps = [
            _mk_map(map_id=1, corridors=0, clean=0, score=0.0),
            _mk_map(map_id=2, corridors=3, clean=SATURATION_PER_MAP, score=0.0),
            _mk_map(map_id=3, corridors=3, clean=0, score=2.0),
            _mk_map(map_id=4, corridors=3, clean=5, score=1.0, near=True),
        ]
        r = build_report(maps, top_n=10)
        assert r.total_maps == 4
        assert r.corridor_owning_maps == 3
        assert [m.map_id for m in r.saturated_maps] == [2]
        assert [m.map_id for m in r.zero_replay_corridor_maps] == [3]
        assert [m.map_id for m in r.near_cohort_boundary_maps] == [4]
        # Backfill should list highest-score maps first; map 2 (saturated)
        # has score 0 and is excluded.
        assert [b.map_id for b in r.backfill_recommendation] == [3, 4]

    def test_render_contains_sections_and_top_entries(self) -> None:
        maps = [
            _mk_map(map_id=3, corridors=3, clean=0, score=2.0),
            _mk_map(map_id=4, corridors=3, clean=5, score=1.0, near=True),
        ]
        r = build_report(maps, top_n=10)
        md = render_markdown(r)
        assert "Replay Coverage Expansion Report" in md
        assert "Saturation cap" in md
        assert "Top-N backfill recommendation" in md
        assert "Map 3" in md
        assert "zero clean replays" in md
