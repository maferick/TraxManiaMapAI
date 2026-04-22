"""Unit tests for corridor diversity metrics. No DB, no files."""
from __future__ import annotations

import pytest

from src.diversity.metrics import (
    CorridorPath,
    build_report,
    compute_interval_diversity,
    jaccard,
    top_k_pairwise_mean_similarity,
)


def _mk(
    *, cid: int, map_id: int = 1, rank: int = 0,
    src_tag: str = "Spawn", src_order: int = 0,
    dst_tag: str = "Goal", dst_order: int = 0,
    cells: list[tuple[int, int, int]] | None = None,
    length: int | None = None,
    virtual: bool = False,
    conf: float | None = None,
    learned: float | None = None,
) -> CorridorPath:
    cells = cells or []
    length = length if length is not None else len(cells)
    return CorridorPath(
        corridor_id=cid, map_id=map_id,
        src_tag=src_tag, src_order=src_order,
        dst_tag=dst_tag, dst_order=dst_order,
        path_rank=rank,
        cells=frozenset(cells),
        path_length=length,
        contains_virtual_edge=virtual,
        corridor_confidence=conf,
        learned_score=learned,
    )


# ---------------------------------------------------------------------
# Jaccard
# ---------------------------------------------------------------------

class TestJaccard:
    def test_identical(self) -> None:
        a = frozenset({(0, 0, 0), (0, 0, 1)})
        assert jaccard(a, a) == 1.0

    def test_disjoint(self) -> None:
        a = frozenset({(0, 0, 0)})
        b = frozenset({(1, 1, 1)})
        assert jaccard(a, b) == 0.0

    def test_partial_overlap(self) -> None:
        a = frozenset({(0, 0, 0), (0, 0, 1), (0, 0, 2)})
        b = frozenset({(0, 0, 1), (0, 0, 2), (0, 0, 3)})
        # 2 shared / 4 union
        assert jaccard(a, b) == pytest.approx(0.5)

    def test_both_empty(self) -> None:
        assert jaccard(frozenset(), frozenset()) == 0.0


# ---------------------------------------------------------------------
# top_k_pairwise_mean_similarity
# ---------------------------------------------------------------------

class TestTopKPairwise:
    def test_identical_corridors_similarity_one(self) -> None:
        c = [(0, 0, i) for i in range(5)]
        paths = [_mk(cid=i, cells=c) for i in range(3)]
        assert top_k_pairwise_mean_similarity(paths, k=3) == pytest.approx(1.0)

    def test_disjoint_corridors_similarity_zero(self) -> None:
        paths = [
            _mk(cid=1, cells=[(0, 0, 0)]),
            _mk(cid=2, cells=[(1, 0, 0)]),
            _mk(cid=3, cells=[(2, 0, 0)]),
        ]
        assert top_k_pairwise_mean_similarity(paths, k=3) == 0.0

    def test_uses_first_k_only(self) -> None:
        # First two identical, third entirely different.
        cells_a = [(0, 0, i) for i in range(4)]
        cells_b = [(9, 9, i) for i in range(4)]
        paths = [_mk(cid=1, cells=cells_a), _mk(cid=2, cells=cells_a),
                 _mk(cid=3, cells=cells_b)]
        assert top_k_pairwise_mean_similarity(paths, k=2) == pytest.approx(1.0)
        # k=3 pulls in the disjoint one, dragging similarity down.
        assert top_k_pairwise_mean_similarity(paths, k=3) < 1.0

    def test_single_path_returns_zero(self) -> None:
        assert top_k_pairwise_mean_similarity(
            [_mk(cid=1, cells=[(0, 0, 0)])], k=3,
        ) == 0.0

    def test_k_less_than_two_rejected(self) -> None:
        with pytest.raises(ValueError):
            top_k_pairwise_mean_similarity([_mk(cid=1)], k=1)


# ---------------------------------------------------------------------
# compute_interval_diversity
# ---------------------------------------------------------------------

class TestIntervalDiversity:
    def test_single_corridor_returns_none(self) -> None:
        assert compute_interval_diversity([_mk(cid=1)], k=3) is None

    def test_diversity_is_one_minus_similarity(self) -> None:
        identical = [(0, 0, i) for i in range(3)]
        disjoint = [(9, 9, i) for i in range(3)]
        paths = [_mk(cid=1, cells=identical), _mk(cid=2, cells=disjoint)]
        d = compute_interval_diversity(paths, k=3)
        assert d is not None
        assert d.mean_pairwise_similarity == 0.0
        assert d.diversity == 1.0

    def test_orders_by_path_rank(self) -> None:
        # Rank-0 and rank-1 identical; rank-2 disjoint.
        # With k=2, should use rank-0 + rank-1 → similarity 1.0
        identical = [(0, 0, i) for i in range(3)]
        disjoint = [(9, 9, i) for i in range(3)]
        paths = [
            _mk(cid=3, rank=2, cells=disjoint),  # deliberately out of order
            _mk(cid=1, rank=0, cells=identical),
            _mk(cid=2, rank=1, cells=identical),
        ]
        d = compute_interval_diversity(paths, k=2)
        assert d is not None
        assert d.mean_pairwise_similarity == pytest.approx(1.0)

    def test_empty_paths_returns_none(self) -> None:
        assert compute_interval_diversity([], k=3) is None


# ---------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------

class TestBuildReport:
    def test_aggregates_counts_and_intervals(self) -> None:
        # Two intervals on two maps; one map has 2 corridors, the
        # other has just 1.
        paths = [
            _mk(cid=1, map_id=10, rank=0, cells=[(0, 0, 0)]),
            _mk(cid=2, map_id=10, rank=1, cells=[(0, 0, 1)]),
            _mk(cid=3, map_id=20, rank=0, cells=[(1, 0, 0)]),
        ]
        report = build_report(paths, k=3)
        assert report.total_corridors == 3
        assert report.corridor_owning_maps == 2
        # Only the first map has ≥ 2 corridors in the interval.
        assert len(report.intervals) == 1
        assert report.intervals[0].map_id == 10

    def test_virtual_edge_fraction_from_rank0(self) -> None:
        paths = [
            _mk(cid=1, map_id=1, rank=0, virtual=True),
            _mk(cid=2, map_id=1, rank=1, virtual=False),   # not rank 0 — ignored
            _mk(cid=3, map_id=2, rank=0, virtual=False),
        ]
        r = build_report(paths, k=3)
        assert r.virtual_edge_fraction_top_rank == pytest.approx(0.5)

    def test_path_length_stdev_computed(self) -> None:
        paths = [
            _mk(cid=i, map_id=i, rank=0, cells=[(0, 0, j) for j in range(i + 2)])
            for i in range(5)
        ]
        r = build_report(paths, k=3)
        assert r.path_length_stdev_top_rank > 0

    def test_heuristic_summary_when_scores_present(self) -> None:
        # Two intervals each with 2 corridors, plus scored.
        paths = [
            _mk(cid=1, map_id=1, rank=0, cells=[(0, 0, 0), (0, 0, 1)], conf=0.9, learned=0.8),
            _mk(cid=2, map_id=1, rank=1, cells=[(1, 1, 0), (1, 1, 1)], conf=0.5, learned=0.7),
            _mk(cid=3, map_id=2, rank=0, cells=[(0, 0, 0)], conf=0.7, learned=0.6),
            _mk(cid=4, map_id=2, rank=1, cells=[(2, 2, 0)], conf=0.3, learned=0.2),
        ]
        r = build_report(paths, k=2)
        assert r.heuristic_summary is not None
        assert r.learned_summary is not None
        assert r.heuristic_summary.intervals_compared == 2
        assert r.learned_summary.intervals_compared == 2

    def test_summary_none_when_scores_missing(self) -> None:
        paths = [
            _mk(cid=1, map_id=1, rank=0, cells=[(0, 0, 0)]),
            _mk(cid=2, map_id=1, rank=1, cells=[(0, 0, 1)]),
        ]
        r = build_report(paths, k=2)
        assert r.heuristic_summary is None
        assert r.learned_summary is None

    def test_cross_map_similarity_quartiles(self) -> None:
        # Three rank-0 corridors, all disjoint — all pairwise Jaccards = 0.
        paths = [
            _mk(cid=1, map_id=1, rank=0, cells=[(0, 0, 0)]),
            _mk(cid=2, map_id=2, rank=0, cells=[(1, 0, 0)]),
            _mk(cid=3, map_id=3, rank=0, cells=[(2, 0, 0)]),
        ]
        r = build_report(paths, k=3)
        q = r.rank0_cross_map_similarity_quartiles
        assert q["n_pairs"] == 3
        assert q["median"] == 0.0
        assert q["mean"] == 0.0
