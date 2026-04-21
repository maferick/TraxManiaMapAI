from __future__ import annotations

import numpy as np
import pytest

from src.route.clusterers import (
    ClustererUnavailableError,
    DbscanClusterer,
    GridClusterer,
    PerSegmentClusterer,
    all_registered,
    create,
    get,
    register,
)
from src.route.clusterers.base import ClusterResult, Clusterer, _reset_for_tests


class TestGridClusterer:
    def test_points_in_same_cell_share_label(self) -> None:
        c = GridClusterer(cell_size=1.0)
        pts = np.array([[0.1, 0.1], [0.2, 0.3], [5.0, 5.0]])
        r = c.fit_predict(pts)
        assert r.labels[0] == r.labels[1]
        assert r.labels[0] != r.labels[2]
        assert r.n_clusters == 2
        assert not r.has_noise

    def test_empty_input(self) -> None:
        r = GridClusterer().fit_predict(np.zeros((0, 2)))
        assert r.labels.shape == (0,)
        assert r.n_clusters == 0

    def test_rejects_nonpositive_cell_size(self) -> None:
        with pytest.raises(ValueError, match="cell_size"):
            GridClusterer(cell_size=0.0)

    def test_rejects_non_2d_input(self) -> None:
        with pytest.raises(ValueError, match="2D"):
            GridClusterer().fit_predict(np.array([1.0, 2.0, 3.0]))

    def test_labels_are_stable_insertion_order(self) -> None:
        c = GridClusterer(cell_size=1.0)
        pts = np.array([[10.5, 10.5], [0.1, 0.1], [10.2, 10.3], [0.5, 0.5]])
        r = c.fit_predict(pts)
        # First unique cell gets label 0, second gets 1, etc.
        assert r.labels[0] == 0
        assert r.labels[1] == 1
        assert r.labels[2] == 0
        assert r.labels[3] == 1


class TestDbscanClusterer:
    def test_construction_without_sklearn(self) -> None:
        # Should succeed even if sklearn isn't installed.
        c = DbscanClusterer(eps=0.5, min_samples=3)
        assert c.name == "dbscan"

    def test_fit_predict_without_sklearn_raises_clear_error(self, monkeypatch) -> None:
        # Force the lazy import to fail regardless of whether sklearn is installed.
        import builtins

        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name.startswith("sklearn"):
                raise ImportError("simulated missing sklearn")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        c = DbscanClusterer()
        with pytest.raises(ClustererUnavailableError, match="scikit-learn"):
            c.fit_predict(np.array([[0.0, 0.0]]))

    def test_rejects_nonpositive_eps(self) -> None:
        with pytest.raises(ValueError, match="eps"):
            DbscanClusterer(eps=0.0)


class TestPerSegmentClusterer:
    def test_disjoint_labels_across_windows(self) -> None:
        # Two tight clusters in window 0, two tight clusters in window 1.
        pts = np.array(
            [
                [0.0, 0.0, 0.0],   # window 0, cluster A
                [0.0, 0.1, 0.0],   # window 0, cluster A
                [0.0, 5.0, 0.0],   # window 0, cluster B (far lateral)
                [100.0, 0.0, 0.0], # window 1, cluster C
                [100.0, 5.0, 0.0], # window 1, cluster D
            ]
        )
        c = PerSegmentClusterer(
            inner_name="grid",
            inner_params={"cell_size": 1.0},
            window_size=10.0,
            window_stride=10.0,
        )
        r = c.fit_predict(pts)
        # All labels unique across windows; each per-window cluster distinct.
        labels = r.labels
        assert labels[0] == labels[1]  # same window-0 cluster
        assert labels[0] != labels[2]  # different window-0 cluster
        assert labels[3] != labels[4]  # different window-1 clusters
        assert labels[0] not in (labels[3], labels[4])  # no collision across windows

    def test_empty_input(self) -> None:
        c = PerSegmentClusterer()
        r = c.fit_predict(np.zeros((0, 3)))
        assert r.labels.shape == (0,)

    def test_rejects_wrong_shape(self) -> None:
        with pytest.raises(ValueError, match="1\\+D"):
            PerSegmentClusterer().fit_predict(np.array([[1.0]]))  # need D>=1

    def test_rejects_bad_window_size(self) -> None:
        with pytest.raises(ValueError, match="window_size"):
            PerSegmentClusterer(window_size=0.0)


class TestRegistry:
    def test_create_by_name(self) -> None:
        c = create("grid", {"cell_size": 2.0})
        assert isinstance(c, GridClusterer)

    def test_create_with_defaults(self) -> None:
        c = create("grid")
        assert isinstance(c, GridClusterer)

    def test_get_unknown_raises(self) -> None:
        with pytest.raises(KeyError, match="no clusterer"):
            get("no-such-clusterer")

    def test_builtin_clusterers_are_registered(self) -> None:
        names = set(all_registered().keys())
        assert {"grid", "dbscan", "per_segment"}.issubset(names)


class TestRegistryDuplicate:
    def test_reregistering_same_class_is_ok(self) -> None:
        # Module import already registered; re-registering the same class
        # should not raise.
        register(GridClusterer)

    def test_registering_different_class_with_same_name_fails(self, monkeypatch) -> None:
        class Other(Clusterer):
            name = "grid"
            version = "9.9.9"

            @classmethod
            def default_params(cls):
                return {}

            def fit_predict(self, points):
                raise NotImplementedError

        with pytest.raises(ValueError, match="already registered"):
            register(Other)


class TestClusterResult:
    def test_noise_detection(self) -> None:
        r = ClusterResult(labels=np.array([0, 1, -1, 1]))
        assert r.n_clusters == 2
        assert r.has_noise

    def test_empty(self) -> None:
        r = ClusterResult(labels=np.zeros((0,), dtype=np.int64))
        assert r.n_clusters == 0
        assert not r.has_noise
