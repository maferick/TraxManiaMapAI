from __future__ import annotations

import pytest

from src.evaluation.dryrun.stats import (
    disagreement_pairs,
    histogram,
    quartiles,
    separation_auc,
)


class TestHistogram:
    def test_empty_input(self) -> None:
        h = histogram([])
        assert sum(h.counts) == 0

    def test_bucket_count_matches_bins(self) -> None:
        h = histogram([0.1, 0.3, 0.5, 0.7, 0.9], bins=5)
        assert len(h.counts) == 5
        assert sum(h.counts) == 5
        assert len(h.edges) == 6

    def test_degenerate_single_value(self) -> None:
        h = histogram([0.5, 0.5, 0.5], bins=4)
        assert sum(h.counts) == 3

    def test_rejects_nonpositive_bins(self) -> None:
        with pytest.raises(ValueError, match="bins"):
            histogram([0.5], bins=0)

    def test_ascii_bar_contains_counts(self) -> None:
        h = histogram([0.1, 0.9], bins=2)
        lines = h.ascii_bar(width=5)
        assert any("  1" in line for line in lines)


class TestQuartiles:
    def test_empty_returns_none(self) -> None:
        assert quartiles([]) is None

    def test_basic_distribution(self) -> None:
        q = quartiles([0.0, 0.25, 0.5, 0.75, 1.0])
        assert q is not None
        assert q.count == 5
        assert q.minimum == 0.0
        assert q.maximum == 1.0
        assert q.median == pytest.approx(0.5)

    def test_as_row_has_seven_columns(self) -> None:
        q = quartiles([0.0, 1.0])
        assert q is not None
        assert len(q.as_row()) == 7


class TestSeparationAuc:
    def test_perfect_separation_positive_above(self) -> None:
        auc = separation_auc([0.9, 0.8, 0.95], [0.1, 0.2, 0.05])
        assert auc == 1.0

    def test_perfect_inverted(self) -> None:
        auc = separation_auc([0.1, 0.2], [0.9, 0.8])
        assert auc == 0.0

    def test_overlap_near_half(self) -> None:
        auc = separation_auc([0.4, 0.5, 0.6], [0.5, 0.4, 0.6])
        assert auc == pytest.approx(0.5, abs=0.1)

    def test_empty_side_returns_none(self) -> None:
        assert separation_auc([], [0.1, 0.2]) is None
        assert separation_auc([0.9], []) is None

    def test_ties_midrank(self) -> None:
        # All ties -> AUC should be exactly 0.5.
        auc = separation_auc([0.5, 0.5], [0.5, 0.5])
        assert auc == 0.5


class TestDisagreementPairs:
    def test_threshold_filters_small_diffs(self) -> None:
        a = {1: 0.8, 2: 0.9}
        b = {1: 0.7, 2: 0.2}
        pairs = disagreement_pairs(a, b, threshold=0.3)
        assert pairs == [(2, 0.9, 0.2)]

    def test_empty_on_identical_scores(self) -> None:
        a = {1: 0.5, 2: 0.5}
        b = {1: 0.5, 2: 0.5}
        assert disagreement_pairs(a, b, threshold=0.1) == []

    def test_ignores_maps_not_in_both(self) -> None:
        a = {1: 0.9}
        b = {2: 0.1}
        assert disagreement_pairs(a, b, threshold=0.1) == []
