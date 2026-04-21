"""DBSCAN adapter.

Lazy-imports ``sklearn.cluster.DBSCAN`` at ``fit_predict`` time. Not
installing scikit-learn is OK — constructing the class still works,
so tests can exercise the factory and registry without the dep. Only
:meth:`fit_predict` fails, with a clear error message.

Install the optional extra with:

    pip install -e ".[learn]"
"""
from __future__ import annotations

from typing import Any

import numpy as np

from src.route.clusterers.base import (
    ClusterResult,
    Clusterer,
    ClustererUnavailableError,
    register,
)


@register
class DbscanClusterer(Clusterer):
    name = "dbscan"
    version = "1.0.0"

    def __init__(
        self,
        *,
        eps: float = 1.0,
        min_samples: int = 5,
        metric: str = "euclidean",
    ) -> None:
        if eps <= 0:
            raise ValueError("eps must be positive")
        if min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        self._eps = float(eps)
        self._min_samples = int(min_samples)
        self._metric = str(metric)

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"eps": 1.0, "min_samples": 5, "metric": "euclidean"}

    def fit_predict(self, points: np.ndarray) -> ClusterResult:
        try:
            from sklearn.cluster import DBSCAN  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ClustererUnavailableError(
                "DBSCAN requires scikit-learn. Install with: "
                'pip install -e ".[learn]"'
            ) from exc
        if points.ndim != 2:
            raise ValueError(f"points must be 2D, got shape {points.shape}")
        if points.shape[0] == 0:
            return ClusterResult(labels=np.zeros((0,), dtype=np.int64))
        model = DBSCAN(eps=self._eps, min_samples=self._min_samples, metric=self._metric)
        labels = model.fit_predict(points).astype(np.int64)
        return ClusterResult(labels=labels)
