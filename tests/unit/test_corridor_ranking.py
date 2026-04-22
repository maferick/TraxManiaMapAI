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
    RidgeRegression,
    _rank_with_ties,
    auc_roc,
    rmse,
    spearman_rank_corr,
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
