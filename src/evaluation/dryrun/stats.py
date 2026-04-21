"""Stats helpers for the dry-run report.

Pure numpy, no scipy dep. The goal is **transparency over
sophistication** — the separation AUC and histograms here exist so a
reader can sanity-check the evaluator behavior before we invest in
any real metric.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import numpy as np


@dataclass(frozen=True)
class HistogramBuckets:
    edges: tuple[float, ...]         # length n+1
    counts: tuple[int, ...]          # length n

    def ascii_bar(self, width: int = 30) -> list[str]:
        if not self.counts:
            return []
        peak = max(self.counts)
        if peak == 0:
            return [f"[{self.edges[i]:+0.3f}, {self.edges[i+1]:+0.3f})  0" for i in range(len(self.counts))]
        lines: list[str] = []
        for i, c in enumerate(self.counts):
            bar = "#" * int(round(width * c / peak))
            lines.append(
                f"[{self.edges[i]:+0.3f}, {self.edges[i+1]:+0.3f})  "
                f"{bar:<{width}}  {c}"
            )
        return lines


def histogram(values: Sequence[float], *, bins: int = 10) -> HistogramBuckets:
    if bins < 1:
        raise ValueError("bins must be >= 1")
    arr = np.asarray(values, dtype=np.float64)
    if arr.size == 0:
        return HistogramBuckets(edges=(0.0, 1.0), counts=(0,))
    lo = float(np.min(arr))
    hi = float(np.max(arr))
    if lo == hi:
        # Degenerate: widen by epsilon to avoid zero-width bins.
        lo -= 0.5
        hi += 0.5
    counts, edges = np.histogram(arr, bins=bins, range=(lo, hi))
    return HistogramBuckets(
        edges=tuple(float(e) for e in edges),
        counts=tuple(int(c) for c in counts),
    )


@dataclass(frozen=True)
class Quartiles:
    count: int
    minimum: float
    q1: float
    median: float
    q3: float
    maximum: float
    mean: float

    def as_row(self) -> list[str]:
        return [
            str(self.count),
            f"{self.minimum:+.4f}",
            f"{self.q1:+.4f}",
            f"{self.median:+.4f}",
            f"{self.q3:+.4f}",
            f"{self.maximum:+.4f}",
            f"{self.mean:+.4f}",
        ]


def quartiles(values: Sequence[float]) -> Quartiles | None:
    arr = np.asarray(values, dtype=np.float64)
    if arr.size == 0:
        return None
    return Quartiles(
        count=int(arr.size),
        minimum=float(np.min(arr)),
        q1=float(np.quantile(arr, 0.25)),
        median=float(np.median(arr)),
        q3=float(np.quantile(arr, 0.75)),
        maximum=float(np.max(arr)),
        mean=float(np.mean(arr)),
    )


def separation_auc(
    positives: Sequence[float], negatives: Sequence[float]
) -> float | None:
    """AUC of a simple "positives score higher than negatives" comparison.

    Equivalent to the normalized Mann-Whitney U statistic. 0.5 means
    no separation; 1.0 means positives strictly above negatives; 0.0
    means the opposite. Returns ``None`` if either side is empty.
    """
    p = np.asarray(positives, dtype=np.float64)
    n = np.asarray(negatives, dtype=np.float64)
    if p.size == 0 or n.size == 0:
        return None
    combined = np.concatenate([p, n])
    # argsort tie-breaks stable; we want midranks for ties.
    order = np.argsort(combined, kind="mergesort")
    ranks = np.empty_like(order, dtype=np.float64)
    # Compute midranks to handle ties cleanly.
    i = 0
    while i < combined.size:
        j = i
        while j + 1 < combined.size and combined[order[j + 1]] == combined[order[i]]:
            j += 1
        midrank = (i + j) / 2.0 + 1.0  # 1-indexed
        for k in range(i, j + 1):
            ranks[order[k]] = midrank
        i = j + 1
    rank_sum_p = float(ranks[: p.size].sum())
    U = rank_sum_p - p.size * (p.size + 1) / 2.0
    return U / (p.size * n.size)


def disagreement_pairs(
    a_scores: dict[int, float],
    b_scores: dict[int, float],
    *,
    threshold: float = 0.2,
) -> list[tuple[int, float, float]]:
    """Return ``(map_id, a_score, b_score)`` where ``|a - b| >= threshold``."""
    shared = a_scores.keys() & b_scores.keys()
    return [
        (map_id, a_scores[map_id], b_scores[map_id])
        for map_id in sorted(shared)
        if abs(a_scores[map_id] - b_scores[map_id]) >= threshold
    ]
