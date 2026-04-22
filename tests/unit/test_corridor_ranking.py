"""Unit tests for corridor ranking: features, labels, model."""
from __future__ import annotations

import numpy as np
import pytest

from src.corridor.ranking.features import (
    FEATURE_NAMES,
    CorridorRow,
    _featurize_one,
    build_feature_matrix,
)
from src.corridor.ranking.labels import synthesize_inverse_rank_labels
from src.corridor.ranking.model import (
    ComparativeTrainingReport,
    RidgeRegression,
    TrainingReport,
    _rank_with_ties,
    _utcnow,
    auc_roc,
    rmse,
    spearman_rank_corr,
)
from src.corridor.ranking.time_envelope_labels import (
    plausibility,
    synthesize_time_envelope_labels,
)
from src.corridor.scoring import EdgeEvidence


def _mk_row(
    corridor_id: int = 1,
    map_id: int = 100,
    path_length: int = 3,
    contains_virtual_edge: bool = False,
    edge_evidences: list[EdgeEvidence] | None = None,
    interval_corridor_count: int = 1,
    path_rank: int = 0,
    corridor_confidence: float | None = None,
    src_tag: str = "Spawn",
    src_order: int = 0,
    dst_tag: str = "Goal",
    dst_order: int = 0,
) -> CorridorRow:
    return CorridorRow(
        corridor_id=corridor_id,
        map_id=map_id,
        src_tag=src_tag,
        src_order=src_order,
        dst_tag=dst_tag,
        dst_order=dst_order,
        path_rank=path_rank,
        path_cells=[(0, 0, i) for i in range(path_length)],
        path_length=path_length,
        contains_virtual_edge=contains_virtual_edge,
        corridor_confidence=corridor_confidence,
        edge_evidences=edge_evidences or [],
        interval_corridor_count=interval_corridor_count,
    )


class TestFeatureExtraction:
    def test_feature_names_exclude_path_rank(self) -> None:
        # Label leak guard: features must NOT include path_rank or
        # is_top_rank since the label is derived from path_rank.
        for n in FEATURE_NAMES:
            assert "rank" not in n.lower() or n == "bias"

    def test_zero_edge_corridor_returns_neutral_features(self) -> None:
        row = _mk_row(path_length=2, edge_evidences=[])
        vec = _featurize_one(row)
        assert vec.shape == (len(FEATURE_NAMES),)
        # Bias is 1.0
        assert vec[0] == 1.0
        # rule_support_fraction is 0 when no edges
        assert vec[FEATURE_NAMES.index("rule_support_fraction")] == 0.0

    def test_all_rule_supported_edges(self) -> None:
        evs = [
            EdgeEvidence(True, 5, 0.5, 0),
            EdgeEvidence(True, 10, 0.8, 3),
        ]
        row = _mk_row(edge_evidences=evs)
        vec = _featurize_one(row)
        i = FEATURE_NAMES.index("rule_support_fraction")
        assert vec[i] == 1.0

    def test_mixed_rule_support(self) -> None:
        evs = [
            EdgeEvidence(True, 5, 0.5, 0),
            EdgeEvidence(False, 0, 0.0, 12),
        ]
        row = _mk_row(edge_evidences=evs)
        vec = _featurize_one(row)
        i = FEATURE_NAMES.index("rule_support_fraction")
        assert vec[i] == 0.5

    def test_virtual_edge_flag(self) -> None:
        row_with = _mk_row(contains_virtual_edge=True)
        row_without = _mk_row(contains_virtual_edge=False)
        i = FEATURE_NAMES.index("contains_virtual_edge")
        assert _featurize_one(row_with)[i] == 1.0
        assert _featurize_one(row_without)[i] == 0.0

    def test_build_feature_matrix_shape(self) -> None:
        rows = [_mk_row(corridor_id=i, path_length=i + 2) for i in range(5)]
        vectors, matrix = build_feature_matrix(rows)
        assert matrix.shape == (5, len(FEATURE_NAMES))
        assert len(vectors) == 5

    def test_empty_input(self) -> None:
        vectors, matrix = build_feature_matrix([])
        assert vectors == []
        assert matrix.shape == (0, len(FEATURE_NAMES))


class TestLabels:
    def test_single_corridor_interval_neutral_label(self) -> None:
        rows = [_mk_row(corridor_id=1, path_rank=0)]
        labels = synthesize_inverse_rank_labels(rows)
        assert labels[1] == 0.5

    def test_two_corridor_interval_binary(self) -> None:
        rows = [
            _mk_row(corridor_id=1, path_rank=0),
            _mk_row(corridor_id=2, path_rank=1),
        ]
        labels = synthesize_inverse_rank_labels(rows)
        assert labels[1] == 1.0
        assert labels[2] == 0.0

    def test_three_corridor_interval_evenly_spaced(self) -> None:
        rows = [
            _mk_row(corridor_id=1, path_rank=0),
            _mk_row(corridor_id=2, path_rank=1),
            _mk_row(corridor_id=3, path_rank=2),
        ]
        labels = synthesize_inverse_rank_labels(rows)
        assert labels[1] == 1.0
        assert labels[2] == 0.5
        assert labels[3] == 0.0

    def test_multi_interval_isolated(self) -> None:
        rows = [
            _mk_row(corridor_id=1, dst_tag="Goal", dst_order=0, path_rank=0),
            _mk_row(corridor_id=2, dst_tag="Goal", dst_order=0, path_rank=1),
            _mk_row(corridor_id=3, dst_tag="Checkpoint", dst_order=1, path_rank=0),
            _mk_row(corridor_id=4, dst_tag="Checkpoint", dst_order=1, path_rank=1),
        ]
        labels = synthesize_inverse_rank_labels(rows)
        # Each interval labels independently
        assert labels[1] == 1.0
        assert labels[3] == 1.0


class TestModel:
    def test_fit_predict_exact_on_linear_data(self) -> None:
        rng = np.random.default_rng(0)
        X = rng.standard_normal((50, 4))
        w_true = np.array([0.5, -1.0, 2.0, 0.1])
        y = X @ w_true
        # With tiny alpha, ridge should recover the weights closely.
        m = RidgeRegression(alpha=0.001)
        m.fit(X, y)
        pred = m.predict(X)
        assert rmse(pred, y) < 0.1

    def test_fit_requires_matching_shapes(self) -> None:
        m = RidgeRegression()
        X = np.zeros((5, 3))
        y = np.zeros(4)
        with pytest.raises(ValueError):
            m.fit(X, y)

    def test_predict_requires_fit(self) -> None:
        m = RidgeRegression()
        with pytest.raises(RuntimeError):
            m.predict(np.zeros((1, 3)))

    def test_serde_round_trip(self) -> None:
        X = np.array([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]])
        y = np.array([1.0, 2.0, 3.0])
        m = RidgeRegression(alpha=0.5, feature_names=("a", "b"))
        m.fit(X, y)
        payload = m.to_dict()
        restored = RidgeRegression.from_dict(payload)
        np.testing.assert_allclose(restored.weights, m.weights)
        assert restored.feature_names == ("a", "b")

    def test_ridge_dampens_compared_to_ols(self) -> None:
        # Highly correlated features → plain OLS would give
        # huge weights with opposite signs. Ridge shrinks them.
        rng = np.random.default_rng(1)
        base = rng.standard_normal(100)
        X = np.column_stack([base, base + 0.001 * rng.standard_normal(100)])
        y = base + 0.1 * rng.standard_normal(100)
        low_alpha = RidgeRegression(alpha=0.001).fit(X, y)
        high_alpha = RidgeRegression(alpha=10.0).fit(X, y)
        assert np.max(np.abs(low_alpha.weights)) > np.max(np.abs(high_alpha.weights))

    # ---- A4: sample-weighted fit ------------------------------------

    def test_uniform_sample_weights_match_unweighted(self) -> None:
        rng = np.random.default_rng(7)
        X = rng.standard_normal((40, 3))
        y = X @ np.array([1.0, -2.0, 0.5])
        unw = RidgeRegression(alpha=0.01).fit(X, y)
        w = RidgeRegression(alpha=0.01).fit(
            X, y, sample_weights=np.ones(40),
        )
        np.testing.assert_allclose(unw.weights, w.weights, atol=1e-10)

    def test_sample_weights_change_fit(self) -> None:
        # Two contradictory clusters: first half says y=+1, second half says y=-1.
        # Weighting first half heavily should pull the fit toward +1.
        X = np.ones((20, 1))
        y = np.concatenate([np.ones(10), -np.ones(10)])
        weights = np.concatenate([np.full(10, 100.0), np.ones(10)])
        biased = RidgeRegression(alpha=0.001).fit(
            X, y, sample_weights=weights,
        )
        unweighted = RidgeRegression(alpha=0.001).fit(X, y)
        # Unweighted mean ≈ 0; heavily biased toward +1 under weights.
        assert abs(unweighted.weights[0]) < 0.05
        assert biased.weights[0] > 0.7

    def test_zero_weights_ignore_samples(self) -> None:
        # First sample is noise; zero-weighting it should recover the
        # signal from the remaining data.
        X = np.array([[0.0], [1.0], [2.0], [3.0]])
        y = np.array([999.0, 1.0, 2.0, 3.0])
        w = np.array([0.0, 1.0, 1.0, 1.0])
        m = RidgeRegression(alpha=0.001).fit(X, y, sample_weights=w)
        # With the noisy sample dropped, weight ≈ 1.0 (line through origin).
        assert abs(m.weights[0] - 1.0) < 0.01

    def test_negative_weights_rejected(self) -> None:
        m = RidgeRegression(alpha=1.0)
        with pytest.raises(ValueError, match="non-negative"):
            m.fit(
                np.zeros((3, 2)), np.zeros(3),
                sample_weights=np.array([1.0, -1.0, 1.0]),
            )

    def test_weight_shape_mismatch_rejected(self) -> None:
        m = RidgeRegression(alpha=1.0)
        with pytest.raises(ValueError, match="sample_weights"):
            m.fit(
                np.zeros((3, 2)), np.zeros(3),
                sample_weights=np.ones(4),
            )


class TestMetrics:
    def test_rmse_zero_on_exact(self) -> None:
        a = np.array([1.0, 2.0, 3.0])
        assert rmse(a, a) == 0.0

    def test_rmse_empty(self) -> None:
        a = np.array([])
        assert rmse(a, a) == 0.0

    def test_rank_corr_perfect_positive(self) -> None:
        a = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        b = np.array([10.0, 20.0, 30.0, 40.0, 50.0])
        assert spearman_rank_corr(a, b) == pytest.approx(1.0)

    def test_rank_corr_perfect_negative(self) -> None:
        a = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        b = np.array([50.0, 40.0, 30.0, 20.0, 10.0])
        assert spearman_rank_corr(a, b) == pytest.approx(-1.0)

    def test_rank_corr_ties_average(self) -> None:
        x = np.array([1.0, 1.0, 2.0, 3.0])
        ranks = _rank_with_ties(x)
        # Two 1s → ranks 1 and 2 → averaged to 1.5 each
        assert ranks[0] == 1.5
        assert ranks[1] == 1.5
        assert ranks[2] == 3.0
        assert ranks[3] == 4.0

    def test_rank_corr_with_nan_drops_pair(self) -> None:
        a = np.array([1.0, 2.0, np.nan, 4.0])
        b = np.array([10.0, 20.0, 30.0, 40.0])
        # Only 3 valid pairs, still perfectly correlated → 1.0
        assert spearman_rank_corr(a, b) == pytest.approx(1.0)

    def test_auc_perfect_separation(self) -> None:
        scores = np.array([0.1, 0.2, 0.9, 0.8])
        labels = np.array([0, 0, 1, 1])
        assert auc_roc(scores, labels) == 1.0

    def test_auc_no_information(self) -> None:
        scores = np.array([0.5, 0.5, 0.5, 0.5])
        labels = np.array([0, 1, 0, 1])
        # All ties → 0.5
        assert auc_roc(scores, labels) == 0.5

    def test_auc_empty_class_returns_half(self) -> None:
        assert auc_roc(np.array([1.0, 2.0]), np.array([0, 0])) == 0.5


class TestPlausibility:
    def test_exact_match_returns_one(self) -> None:
        # path_length=3 cells × 32m / 30 m/s × 1000ms = 3200 ms expected.
        # observed = 3200 → rel_err = 0 → exp(0) = 1.0
        p = plausibility(path_length_cells=3, observed_elapsed_ms=3200.0)
        assert p == pytest.approx(1.0)

    def test_non_positive_inputs_return_zero(self) -> None:
        assert plausibility(0, 5000.0) == 0.0
        assert plausibility(-1, 5000.0) == 0.0
        assert plausibility(3, 0.0) == 0.0
        assert plausibility(3, -500.0) == 0.0
        assert plausibility(3, 5000.0, speed_prior_m_s=0.0) == 0.0
        assert plausibility(3, 5000.0, block_size_m=0.0) == 0.0

    def test_monotone_decay_on_length_mismatch(self) -> None:
        # Observed = 3200 ms (fits 3 cells exactly at 30 m/s).
        # As path_length diverges from 3, plausibility should decrease.
        p_exact = plausibility(3, 3200.0)
        p_plus = plausibility(6, 3200.0)
        p_minus = plausibility(1, 3200.0)
        assert p_exact > p_plus
        assert p_exact > p_minus

    def test_values_stay_in_unit_interval(self) -> None:
        # Exponential decay on non-negative relative error → (0, 1].
        for length, obs in [(1, 100.0), (50, 500.0), (5, 1e6)]:
            p = plausibility(length, obs)
            assert 0.0 <= p <= 1.0

    def test_speed_prior_changes_expected_time(self) -> None:
        # Doubling the speed prior halves expected time; plausibility
        # changes accordingly. At speed=30, path_length=3 → 3200ms exact.
        # At speed=60, path_length=3 → 1600ms expected; observed=3200ms
        # gives rel_err=0.5, plausibility=exp(-0.5)≈0.606.
        p60 = plausibility(3, 3200.0, speed_prior_m_s=60.0)
        assert p60 == pytest.approx(pytest.approx(0.6065, abs=1e-3))


class TestTimeEnvelopeLabels:
    def test_no_replay_data_omits_corridor(self) -> None:
        rows = [_mk_row(corridor_id=1, map_id=100, path_length=3)]
        labels = synthesize_time_envelope_labels(rows, map_mean_interval_ms={})
        assert labels == {}

    def test_with_replay_data_produces_label(self) -> None:
        rows = [_mk_row(corridor_id=1, map_id=100, path_length=3)]
        # map_id=100 has observed mean interval 3200ms → exact match at
        # default 30 m/s speed prior → label 1.0.
        labels = synthesize_time_envelope_labels(
            rows, map_mean_interval_ms={100: 3200.0},
        )
        assert labels[1] == pytest.approx(1.0)

    def test_partial_coverage_only_labels_mapped(self) -> None:
        rows = [
            _mk_row(corridor_id=1, map_id=100, path_length=3),
            _mk_row(corridor_id=2, map_id=200, path_length=3),
        ]
        labels = synthesize_time_envelope_labels(
            rows, map_mean_interval_ms={100: 3200.0},
        )
        assert 1 in labels
        assert 2 not in labels  # map 200 has no mean — corridor dropped

    def test_labels_in_unit_interval(self) -> None:
        rows = [
            _mk_row(corridor_id=1, map_id=1, path_length=1),
            _mk_row(corridor_id=2, map_id=1, path_length=10),
            _mk_row(corridor_id=3, map_id=1, path_length=100),
        ]
        labels = synthesize_time_envelope_labels(
            rows, map_mean_interval_ms={1: 3200.0},
        )
        for v in labels.values():
            assert 0.0 <= v <= 1.0


class TestComparativeTrainingReport:
    def _mk_report(self, scheme: str) -> TrainingReport:
        return TrainingReport(
            label_scheme=scheme,
            trained_at=_utcnow(),
            total_rows=10, train_rows=8, test_rows=2,
            alpha=1.0, feature_names=["bias"], weights=[0.1],
            train_rmse=0.1, test_rmse=0.2,
            test_rank_corr=0.3, heuristic_rank_corr=0.0,
            auc_learned=0.7, auc_heuristic=0.5, auc_delta=0.2,
            n_maps_learned=20, n_maps_heuristic=20,
            random_seed=42,
        )

    def test_serializes_both_schemes(self) -> None:
        rep = ComparativeTrainingReport(
            inverse_rank=self._mk_report("inverse_rank"),
            time_envelope=self._mk_report("time_envelope"),
            map_mean_interval_ms_count=42,
        )
        payload = rep.to_dict()
        assert payload["inverse_rank"]["label_scheme"] == "inverse_rank"
        assert payload["time_envelope"]["label_scheme"] == "time_envelope"
        assert payload["map_mean_interval_ms_count"] == 42

    def test_time_envelope_can_be_none(self) -> None:
        rep = ComparativeTrainingReport(
            inverse_rank=self._mk_report("inverse_rank"),
            time_envelope=None,
            map_mean_interval_ms_count=0,
        )
        payload = rep.to_dict()
        assert payload["time_envelope"] is None
