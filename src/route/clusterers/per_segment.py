"""Sliding-window clusterer over an ordering coordinate.

Wraps any inner :class:`Clusterer` and applies it within overlapping
windows along the first column of the input array (interpreted as an
ordering coordinate, typically arc-length ``s``). Labels are
disjoint across windows — a point in window *k* gets a label offset
so it never collides with window *k*'s neighbors.

Points may fall in multiple windows; they are labeled from the first
window whose center is closest (smallest |s - window_center|). This
avoids a single point getting two labels.

Convention: input shape is ``(M, 1 + D)`` where column 0 is the
ordering coordinate and columns 1..D are the clustering features.
"""
from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from src.route.clusterers.base import ClusterResult, Clusterer, create, register


@register
class PerSegmentClusterer(Clusterer):
    name = "per_segment"
    version = "1.0.0"

    def __init__(
        self,
        *,
        inner_name: str = "grid",
        inner_params: Mapping[str, Any] | None = None,
        window_size: float = 20.0,
        window_stride: float = 10.0,
    ) -> None:
        if window_size <= 0:
            raise ValueError("window_size must be positive")
        if window_stride <= 0:
            raise ValueError("window_stride must be positive")
        self._inner_name = inner_name
        self._inner_params = dict(inner_params) if inner_params else None
        self._window_size = float(window_size)
        self._window_stride = float(window_stride)

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {
            "inner_name": "grid",
            "inner_params": None,
            "window_size": 20.0,
            "window_stride": 10.0,
        }

    def fit_predict(self, points: np.ndarray) -> ClusterResult:
        if points.ndim != 2 or points.shape[1] < 2:
            raise ValueError(
                f"per_segment input must have shape (M, 1+D) with D>=1, got {points.shape}"
            )
        if points.shape[0] == 0:
            return ClusterResult(labels=np.zeros((0,), dtype=np.int64))

        order = points[:, 0]
        features = points[:, 1:]

        lo = float(order.min())
        hi = float(order.max())
        # Assign each point to exactly one window: the one whose center is closest.
        # Window k covers [lo + k*stride, lo + k*stride + size]; center at
        # lo + k*stride + size/2.
        n_windows = max(
            1, int(np.ceil((hi - lo - self._window_size) / self._window_stride)) + 1
        )
        centers = lo + np.arange(n_windows) * self._window_stride + self._window_size / 2.0
        assignments = np.argmin(np.abs(order[:, None] - centers[None, :]), axis=1)

        labels = np.empty(points.shape[0], dtype=np.int64)
        next_label = 0
        for w in range(n_windows):
            mask = assignments == w
            if not mask.any():
                continue
            inner = create(self._inner_name, self._inner_params)
            window_feats = features[mask]
            sub = inner.fit_predict(window_feats)
            shifted = np.where(sub.labels < 0, -1, sub.labels + next_label)
            labels[mask] = shifted
            positive = shifted[shifted >= 0]
            if positive.size:
                next_label = int(positive.max()) + 1
        return ClusterResult(labels=labels)
