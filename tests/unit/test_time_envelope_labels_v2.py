"""Unit tests for time_envelope v2 — aggregation, outlier rejection,
label value/quality, provenance. No DB, no filesystem."""
from __future__ import annotations

import math

import pytest

from src.corridor.ranking.features import CorridorRow
from src.corridor.ranking.time_envelope_labels_v2 import (
    MapIntervalStats,
    _aggregate,
    _extract_gaps,
    _reject_outliers,
    build_metadata,
    compute_interval_stats,
    synthesize_time_envelope_v2_labels,
)


def _mk_row(
    *, corridor_id: int, map_id: int = 100, path_length: int = 3,
) -> CorridorRow:
    return CorridorRow(
        corridor_id=corridor_id,
        map_id=map_id,
        src_tag="Spawn", src_order=0,
        dst_tag="Goal", dst_order=0,
        path_rank=0,
        path_cells=[(0, 0, i) for i in range(path_length)],
        path_length=path_length,
        contains_virtual_edge=False,
        corridor_confidence=None,
        edge_evidences=[],
        interval_corridor_count=1,
    )


# ---------------------------------------------------------------------
# Aggregation primitives
# ---------------------------------------------------------------------

class TestAggregate:
    def test_mean(self) -> None:
        assert _aggregate([1.0, 2.0, 3.0, 4.0], "mean", 0.1) == pytest.approx(2.5)

    def test_median(self) -> None:
        assert _aggregate([1.0, 2.0, 3.0, 100.0], "median", 0.1) == pytest.approx(2.5)
        # Median is robust to the single outlier; mean would explode.
        assert _aggregate([1.0, 2.0, 3.0, 100.0], "mean", 0.1) > 25.0

    def test_trimmed_mean_drops_extremes(self) -> None:
        # 10 values, trim q=0.1 → drop 1 each end → keep middle 8.
        vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 100.0]
        trimmed = _aggregate(vals, "trimmed_mean", 0.1)
        # Middle 8 = [2,3,4,5,6,7,8,9] → mean 5.5
        assert trimmed == pytest.approx(5.5)

    def test_trimmed_mean_q_zero_equals_mean(self) -> None:
        vals = [1.0, 2.0, 3.0]
        assert _aggregate(vals, "trimmed_mean", 0.0) == pytest.approx(2.0)

    def test_trimmed_q_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError):
            _aggregate([1.0, 2.0], "trimmed_mean", 0.5)
        with pytest.raises(ValueError):
            _aggregate([1.0, 2.0], "trimmed_mean", -0.1)

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            _aggregate([], "mean", 0.1)

    def test_unknown_method_raises(self) -> None:
        with pytest.raises(ValueError):
            _aggregate([1.0], "bogus", 0.1)  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# Outlier rejection
# ---------------------------------------------------------------------

class TestRejectOutliers:
    def test_sigma_none_passes_through(self) -> None:
        assert _reject_outliers([1.0, 100.0], None) == [1.0, 100.0]

    def test_drops_value_outside_sigma(self) -> None:
        # [10, 10, 10, 10, 100] — mean ≈ 28, stdev ≈ 40 — 100 is only
        # ~1.8σ away and stays. Push harder:
        # With 9×10 and one 100, mean≈19, stdev≈28.4, |100-19|/28.4≈2.85σ — still inside 3σ.
        # Use a clearer case:
        kept = _reject_outliers([10.0] * 50 + [10000.0], 3.0)
        assert 10000.0 not in kept
        assert all(v == 10.0 for v in kept)

    def test_preserves_sample_without_outliers(self) -> None:
        kept = _reject_outliers([10.0, 10.1, 9.9, 10.05], 3.0)
        assert len(kept) == 4

    def test_short_sample_passes_through(self) -> None:
        # With only 1 value, can't compute stdev → pass through.
        assert _reject_outliers([5.0], 3.0) == [5.0]

    def test_zero_stdev_passes_through(self) -> None:
        # All equal → stdev 0 → can't reject; everything stays.
        assert _reject_outliers([5.0, 5.0, 5.0], 3.0) == [5.0, 5.0, 5.0]


# ---------------------------------------------------------------------
# Gap extraction
# ---------------------------------------------------------------------

class TestExtractGaps:
    def test_standard_checkpoint_times(self) -> None:
        # Spawn at t=0; CPs at [5000, 10000, 15000] → gaps [5000, 5000, 5000].
        gaps = _extract_gaps([5000.0, 10000.0, 15000.0])
        assert gaps == [5000.0, 5000.0, 5000.0]

    def test_drops_nonmonotonic_gaps(self) -> None:
        # CP times [5000, 4000] → second gap negative → dropped.
        gaps = _extract_gaps([5000.0, 4000.0])
        assert gaps == [5000.0]

    def test_empty_on_short_input(self) -> None:
        assert _extract_gaps([]) == []
        assert _extract_gaps([5000.0]) == []


# ---------------------------------------------------------------------
# compute_interval_stats — the main aggregator
# ---------------------------------------------------------------------

class TestComputeIntervalStats:
    def test_stable_driver_has_high_quality(self) -> None:
        # All gaps ≈ 5000 ms → CV ≈ 0 → quality ≈ 1.
        stats = compute_interval_stats(
            42, [5000.0] * 10,
            method="mean", trimmed_q=0.1, outlier_sigma=3.0,
        )
        assert stats is not None
        assert stats.label_quality_weight == pytest.approx(1.0, abs=0.01)

    def test_noisy_driver_has_lower_quality(self) -> None:
        gaps = [3000.0, 4000.0, 5000.0, 6000.0, 7000.0, 15000.0]
        stats = compute_interval_stats(
            42, gaps,
            method="mean", trimmed_q=0.1, outlier_sigma=None,
        )
        assert stats is not None
        # CV = stdev/mean; should be non-trivial.
        assert stats.coefficient_of_variation > 0.2
        assert stats.label_quality_weight < 0.9

    def test_median_robust_to_outlier(self) -> None:
        gaps = [5000.0, 5000.0, 5000.0, 5000.0, 5000.0, 50000.0]
        mean_stats = compute_interval_stats(
            42, gaps, method="mean", trimmed_q=0.1, outlier_sigma=None,
        )
        median_stats = compute_interval_stats(
            42, gaps, method="median", trimmed_q=0.1, outlier_sigma=None,
        )
        assert mean_stats is not None and median_stats is not None
        # Median ignores the 50000; mean is dragged up.
        assert median_stats.aggregated_interval_ms == pytest.approx(5000.0)
        assert mean_stats.aggregated_interval_ms > 10000.0

    def test_outlier_rejection_tightens_mean(self) -> None:
        gaps = [5000.0] * 50 + [100000.0]
        without_rejection = compute_interval_stats(
            42, gaps, method="mean", trimmed_q=0.1, outlier_sigma=None,
        )
        with_rejection = compute_interval_stats(
            42, gaps, method="mean", trimmed_q=0.1, outlier_sigma=3.0,
        )
        assert without_rejection is not None and with_rejection is not None
        # Rejection should remove the spike, tightening the mean.
        assert with_rejection.aggregated_interval_ms < without_rejection.aggregated_interval_ms
        assert with_rejection.replay_count_used == 50

    def test_empty_after_rejection_returns_none(self) -> None:
        # Degenerate: 2 values, no stdev → pass-through → fine. But an
        # empty input → None.
        assert compute_interval_stats(
            42, [], method="mean", trimmed_q=0.1, outlier_sigma=3.0,
        ) is None


# ---------------------------------------------------------------------
# synthesize_time_envelope_v2_labels
# ---------------------------------------------------------------------

class TestSynthesizeV2:
    def test_no_stats_means_no_labels(self) -> None:
        rows = [_mk_row(corridor_id=1, map_id=100)]
        labels, quality = synthesize_time_envelope_v2_labels(rows, {})
        assert labels == {}
        assert quality == {}

    def test_labels_and_quality_populated(self) -> None:
        # path_length=3 cells × 32m / 30 m/s × 1000 = 3200 ms expected.
        # Choose observed = 3200 → plausibility 1.0.
        stats = MapIntervalStats(
            map_id=100,
            aggregated_interval_ms=3200.0,
            interval_stdev_ms=0.0,
            replay_count_used=10,
            coefficient_of_variation=0.0,
            label_quality_weight=1.0,
        )
        rows = [_mk_row(corridor_id=1, map_id=100, path_length=3)]
        labels, quality = synthesize_time_envelope_v2_labels(rows, {100: stats})
        assert labels[1] == pytest.approx(1.0)
        assert quality[1] == pytest.approx(1.0)

    def test_partial_coverage_only_labels_mapped(self) -> None:
        stats = MapIntervalStats(
            map_id=100, aggregated_interval_ms=3200.0, interval_stdev_ms=100.0,
            replay_count_used=5, coefficient_of_variation=0.03,
            label_quality_weight=0.97,
        )
        rows = [
            _mk_row(corridor_id=1, map_id=100, path_length=3),
            _mk_row(corridor_id=2, map_id=200, path_length=3),
        ]
        labels, quality = synthesize_time_envelope_v2_labels(rows, {100: stats})
        assert 1 in labels and 2 not in labels
        assert 1 in quality and 2 not in quality


# ---------------------------------------------------------------------
# Metadata provenance
# ---------------------------------------------------------------------

class TestBuildMetadata:
    def test_captures_parameters_and_counts(self) -> None:
        stats_map = {
            42: MapIntervalStats(42, 3000.0, 100.0, 7, 0.033, 0.967),
            43: MapIntervalStats(43, 5000.0,  50.0, 3, 0.010, 0.990),
        }
        md = build_metadata(
            stats_map,
            method="trimmed_mean", trimmed_q=0.1,
            outlier_sigma=3.0, speed_prior_m_s=30.0,
        )
        assert md.aggregation_method == "trimmed_mean"
        assert md.trimmed_q == 0.1
        assert md.outlier_rejection_sigma == 3.0
        assert md.speed_prior_m_s == 30.0
        assert md.replay_count_per_map == {42: 7, 43: 3}
        # to_dict round-trips the same fields.
        d = md.to_dict()
        assert d["aggregation_method"] == "trimmed_mean"
        assert d["replay_count_per_map"] == {42: 7, 43: 3}
