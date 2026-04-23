"""Unit tests for the PR B synthetic-score helpers.

Pure functions — no DB, no filesystem. Exercises the threshold
bands, saturation behaviour, missing-input handling, and trend
classification.
"""
from __future__ import annotations

import pytest

from src.learning.scores import (
    QualityInputs,
    ReadinessReport,
    TrendSample,
    ai_quality_score,
    generation_readiness,
    trend_direction,
    variety_score,
)


# ---------------------------------------------------------------------
# ai_quality_score
# ---------------------------------------------------------------------

class TestAiQualityScore:
    def test_all_axes_at_ceiling_saturates_to_one(self) -> None:
        q = ai_quality_score(QualityInputs(
            test_rank_corr=0.80,     # above 0.50 ceil
            pred_stdev_ratio=1.50,   # above 1.00 ceil
            auc_delta=0.50,          # above 0.25 ceil
        ))
        assert q == pytest.approx(1.0)

    def test_all_axes_at_floor_floors_to_zero(self) -> None:
        q = ai_quality_score(QualityInputs(
            test_rank_corr=0.05,     # below 0.10 floor
            pred_stdev_ratio=0.20,   # below 0.30 floor
            auc_delta=-0.10,         # below 0.0 floor
        ))
        assert q == pytest.approx(0.0)

    def test_single_axis_mid_range(self) -> None:
        # rank_corr 0.30 is halfway between floor 0.10 and ceil 0.50 → axis=0.5
        q = ai_quality_score(QualityInputs(test_rank_corr=0.30))
        assert q == pytest.approx(0.5)

    def test_missing_axes_skipped(self) -> None:
        # Only rank_corr given → score is just that axis.
        q = ai_quality_score(QualityInputs(test_rank_corr=0.30))
        assert q == pytest.approx(0.5)
        # Two axes given → mean of both.
        q2 = ai_quality_score(QualityInputs(
            test_rank_corr=0.30,           # axis=0.5
            pred_stdev_ratio=1.0,          # axis=1.0 (at ceiling)
        ))
        assert q2 == pytest.approx(0.75)

    def test_all_inputs_missing_returns_none(self) -> None:
        assert ai_quality_score(QualityInputs()) is None

    def test_monotonic_in_each_axis(self) -> None:
        qa = ai_quality_score(QualityInputs(test_rank_corr=0.15)) or 0
        qb = ai_quality_score(QualityInputs(test_rank_corr=0.45)) or 0
        assert qa < qb


# ---------------------------------------------------------------------
# variety_score
# ---------------------------------------------------------------------

class TestVarietyScore:
    def test_none_input_returns_none(self) -> None:
        assert variety_score(None) is None

    def test_zero_and_positive_delta_is_one(self) -> None:
        assert variety_score(0.0) == 1.0
        assert variety_score(0.05) == 1.0

    def test_severe_collapse_is_zero(self) -> None:
        assert variety_score(-0.20) == 0.0
        assert variety_score(-0.50) == 0.0

    def test_midpoint_halves(self) -> None:
        assert variety_score(-0.10) == pytest.approx(0.5)

    def test_mild_collapse_still_high(self) -> None:
        # Post-A4 observed delta_median -0.04 should still score
        # comfortably above 0.75.
        assert (variety_score(-0.04) or 0) > 0.75
        # And exactly at -0.04 should be 0.8 within float precision.
        assert variety_score(-0.04) == pytest.approx(0.8, abs=1e-6)


# ---------------------------------------------------------------------
# generation_readiness
# ---------------------------------------------------------------------

class TestGenerationReadiness:
    def test_all_gates_pass(self) -> None:
        r = generation_readiness(
            ai_quality=0.60, variety=0.90,
            label_coverage=0.50, learned_coverage=0.95,
        )
        assert r.ready is True
        assert r.fraction == 1.0
        assert all("OK" in reason for reason in r.reasons)

    def test_one_gate_fails_blocks_ready(self) -> None:
        r = generation_readiness(
            ai_quality=0.20,  # below 0.40 floor
            variety=0.90,
            label_coverage=0.50,
            learned_coverage=0.95,
        )
        assert r.ready is False
        assert r.fraction == pytest.approx(3 / 4)
        assert any("below floor" in reason for reason in r.reasons)

    def test_missing_inputs_fail_closed(self) -> None:
        # Missing inputs = failed gates (we don't assume absent = OK).
        r = generation_readiness(
            ai_quality=None, variety=0.90,
            label_coverage=0.50, learned_coverage=0.95,
        )
        assert r.ready is False
        assert r.fraction == pytest.approx(3 / 4)
        assert any("data unavailable" in reason for reason in r.reasons)

    def test_all_inputs_missing(self) -> None:
        r = generation_readiness(
            ai_quality=None, variety=None,
            label_coverage=None, learned_coverage=None,
        )
        assert r.ready is False
        assert r.fraction == 0.0

    def test_edge_of_floor_passes(self) -> None:
        # Exactly at the floor value is considered passing (>=).
        r = generation_readiness(
            ai_quality=0.40, variety=0.70,
            label_coverage=0.10, learned_coverage=0.80,
        )
        assert r.ready is True


# ---------------------------------------------------------------------
# trend_direction
# ---------------------------------------------------------------------

class TestTrendDirection:
    def test_insufficient_data(self) -> None:
        assert trend_direction([]) == "unknown"
        assert trend_direction(
            [TrendSample(0, 0.5)]
        ) == "unknown"

    def test_improving(self) -> None:
        samples = [
            TrendSample(0, 0.20),
            TrendSample(1, 0.25),
            TrendSample(2, 0.30),
            TrendSample(3, 0.55),
            TrendSample(4, 0.60),
            TrendSample(5, 0.65),
        ]
        assert trend_direction(samples) == "improving"

    def test_worsening(self) -> None:
        samples = [
            TrendSample(0, 0.65),
            TrendSample(1, 0.60),
            TrendSample(2, 0.55),
            TrendSample(3, 0.30),
            TrendSample(4, 0.25),
            TrendSample(5, 0.20),
        ]
        assert trend_direction(samples) == "worsening"

    def test_flat(self) -> None:
        samples = [TrendSample(i, 0.50) for i in range(6)]
        assert trend_direction(samples) == "flat"

    def test_small_drift_is_flat(self) -> None:
        samples = [
            TrendSample(0, 0.50),
            TrendSample(1, 0.51),
            TrendSample(2, 0.50),
            TrendSample(3, 0.51),
            TrendSample(4, 0.52),
            TrendSample(5, 0.50),
        ]
        assert trend_direction(samples) == "flat"

    def test_missing_samples_skipped(self) -> None:
        samples = [
            TrendSample(0, None),
            TrendSample(1, 0.20),
            TrendSample(2, 0.30),
            TrendSample(3, 0.65),
            TrendSample(4, 0.70),
        ]
        assert trend_direction(samples) == "improving"
