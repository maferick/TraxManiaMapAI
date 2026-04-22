"""Unit tests for corridor-ranking score-spread diagnostics."""
from __future__ import annotations

import numpy as np
import pytest

from src.corridor.ranking.diagnostics import (
    feature_ablation,
    label_distribution_summary,
    regularization_sweep,
)
from src.corridor.ranking.features import CorridorFeatureVector


def _mk_vectors(n: int, start_map_id: int = 1) -> list[CorridorFeatureVector]:
    return [
        CorridorFeatureVector(
            corridor_id=i,
            map_id=start_map_id + (i // 5),
            features=np.zeros(3),
            corridor_confidence=None,
        )
        for i in range(n)
    ]


class TestLabelDistributionSummary:
    def test_empty_labels_returns_none(self) -> None:
        assert label_distribution_summary("test", {}) is None

    def test_single_label_zero_stdev(self) -> None:
        s = label_distribution_summary("test", {1: 0.5})
        assert s is not None
        assert s.count == 1
        assert s.stdev == 0.0
        assert s.mean == 0.5

    def test_quartiles_correct(self) -> None:
        s = label_distribution_summary(
            "test", {i: float(i) for i in range(1, 5)}
        )
        assert s is not None
        assert s.minimum == 1.0
        assert s.maximum == 4.0
        assert s.median == pytest.approx(2.5)
        assert s.stdev > 0

    def test_carries_scheme_name(self) -> None:
        s = label_distribution_summary("xyz", {1: 0.1})
        assert s is not None
        assert s.label_scheme == "xyz"


class TestRegularizationSweep:
    def test_weight_l2_decreases_with_alpha(self) -> None:
        # Construct data where a non-trivial fit exists.
        rng = np.random.default_rng(0)
        X = np.column_stack([np.ones(60), rng.standard_normal((60, 2))])
        y = X[:, 1] * 2.0 + rng.standard_normal(60) * 0.1
        vectors = _mk_vectors(60)
        rows = regularization_sweep(
            vectors=vectors, X=X, y=y,
            alphas=[0.001, 1.0, 1000.0],
            feature_names=("bias", "a", "b"),
        )
        # Strongly regularized solution → smaller weights.
        assert rows[0].weight_l2_norm > rows[1].weight_l2_norm > rows[2].weight_l2_norm

    def test_pred_stdev_decreases_with_alpha_on_signal_data(self) -> None:
        rng = np.random.default_rng(1)
        X = np.column_stack([np.ones(60), rng.standard_normal((60, 2))])
        y = X[:, 1] * 3.0
        vectors = _mk_vectors(60)
        rows = regularization_sweep(
            vectors=vectors, X=X, y=y,
            alphas=[0.001, 10.0],
            feature_names=("bias", "a", "b"),
        )
        assert rows[0].pred_stdev_all > rows[1].pred_stdev_all

    def test_auc_none_when_no_cohorts(self) -> None:
        rng = np.random.default_rng(2)
        X = rng.standard_normal((30, 2))
        y = rng.standard_normal(30)
        vectors = _mk_vectors(30)
        rows = regularization_sweep(
            vectors=vectors, X=X, y=y,
            alphas=[1.0], feature_names=("a", "b"),
        )
        assert rows[0].auc_learned is None

    def test_auc_reported_with_cohorts(self) -> None:
        rng = np.random.default_rng(3)
        X = np.column_stack([np.ones(30), rng.standard_normal((30, 2))])
        y = X[:, 1] * 2.0
        vectors = _mk_vectors(30, start_map_id=100)
        # Build cohorts over map ids that actually exist in vectors.
        all_maps = sorted({v.map_id for v in vectors})
        pos_ids = set(all_maps[:2])
        neg_ids = set(all_maps[2:4])
        rows = regularization_sweep(
            vectors=vectors, X=X, y=y,
            alphas=[1.0],
            feature_names=("bias", "a", "b"),
            pos_ids=pos_ids, neg_ids=neg_ids,
        )
        assert rows[0].auc_learned is not None
        assert rows[0].n_auc_maps > 0


class TestFeatureAblation:
    def test_carrier_feature_has_negative_stdev_delta(self) -> None:
        # Feature 1 is the only one with signal. Ablating it should
        # drop predicted stdev by a lot.
        rng = np.random.default_rng(4)
        X = np.column_stack([
            np.ones(60),
            rng.standard_normal(60),
            rng.standard_normal(60) * 0.001,  # essentially noise
        ])
        y = X[:, 1] * 2.0
        vectors = _mk_vectors(60)
        baseline, rows = feature_ablation(
            vectors=vectors, X=X, y=y, alpha=0.1,
            feature_names=("bias", "signal", "noise"),
        )
        by_name = {r.feature_name: r for r in rows}
        # Ablating 'signal' should crush prediction stdev.
        assert by_name["signal"].pred_stdev_delta < -0.5
        # Ablating 'noise' should barely move it.
        assert abs(by_name["noise"].pred_stdev_delta) < 0.1

    def test_raises_on_feature_name_shape_mismatch(self) -> None:
        X = np.zeros((5, 3))
        y = np.zeros(5)
        with pytest.raises(ValueError):
            feature_ablation(
                vectors=_mk_vectors(5), X=X, y=y, alpha=1.0,
                feature_names=("a", "b"),   # 2 names, 3 cols
            )

    def test_returns_row_per_feature(self) -> None:
        rng = np.random.default_rng(5)
        X = np.column_stack([np.ones(30), rng.standard_normal((30, 2))])
        y = rng.standard_normal(30)
        baseline, rows = feature_ablation(
            vectors=_mk_vectors(30), X=X, y=y, alpha=1.0,
            feature_names=("bias", "a", "b"),
        )
        assert len(rows) == 3
        assert [r.feature_name for r in rows] == ["bias", "a", "b"]


class TestSampleWeightsThreadedThroughDiagnostics:
    """A4: sample_weights propagate through regularization_sweep and
    feature_ablation into the underlying RidgeRegression.fit."""

    def test_uniform_weights_match_unweighted_sweep(self) -> None:
        rng = np.random.default_rng(11)
        X = np.column_stack([np.ones(60), rng.standard_normal((60, 2))])
        y = X[:, 1] * 2.0 + 0.05 * rng.standard_normal(60)
        vectors = _mk_vectors(60)
        unw = regularization_sweep(
            vectors=vectors, X=X, y=y,
            alphas=[1.0], feature_names=("bias", "a", "b"),
        )
        with_unit_w = regularization_sweep(
            vectors=vectors, X=X, y=y,
            alphas=[1.0], feature_names=("bias", "a", "b"),
            sample_weights=np.ones(60),
        )
        assert unw[0].pred_stdev_all == pytest.approx(
            with_unit_w[0].pred_stdev_all, rel=1e-10,
        )

    def test_non_uniform_weights_change_fit(self) -> None:
        rng = np.random.default_rng(12)
        X = np.column_stack([np.ones(60), rng.standard_normal((60, 2))])
        y = X[:, 1] * 2.0 + 0.05 * rng.standard_normal(60)
        vectors = _mk_vectors(60)
        unw = regularization_sweep(
            vectors=vectors, X=X, y=y,
            alphas=[1.0], feature_names=("bias", "a", "b"),
        )
        weights = np.concatenate([np.full(30, 10.0), np.ones(30)])
        weighted = regularization_sweep(
            vectors=vectors, X=X, y=y,
            alphas=[1.0], feature_names=("bias", "a", "b"),
            sample_weights=weights,
        )
        # Fit differs → predictions differ → stdev differs.
        assert unw[0].pred_stdev_all != pytest.approx(
            weighted[0].pred_stdev_all, rel=1e-6,
        )
