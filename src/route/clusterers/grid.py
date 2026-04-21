"""Grid-bucket clusterer. Default, zero heavy deps, deterministic.

Points falling in the same axis-aligned cell of size ``cell_size``
form one cluster. No noise label is emitted — every point belongs to
its cell's cluster. Suitable for coarse branch detection on
low-dimensional (2D / 3D) replay-offset spaces.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from src.route.clusterers.base import ClusterResult, Clusterer, register


@register
class GridClusterer(Clusterer):
    name = "grid"
    version = "1.0.0"

    def __init__(self, *, cell_size: float = 1.0) -> None:
        if cell_size <= 0:
            raise ValueError("cell_size must be positive")
        self._cell_size = float(cell_size)

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"cell_size": 1.0}

    def fit_predict(self, points: np.ndarray) -> ClusterResult:
        if points.ndim != 2:
            raise ValueError(f"points must be 2D, got shape {points.shape}")
        if points.shape[0] == 0:
            return ClusterResult(labels=np.zeros((0,), dtype=np.int64))

        cells = np.floor(points / self._cell_size).astype(np.int64)
        # Map each unique cell tuple to a label; stable on insertion order.
        seen: dict[tuple[int, ...], int] = {}
        labels = np.empty(points.shape[0], dtype=np.int64)
        for i, row in enumerate(cells):
            key = tuple(int(v) for v in row)
            label = seen.get(key)
            if label is None:
                label = len(seen)
                seen[key] = label
            labels[i] = label
        return ClusterResult(labels=labels)
