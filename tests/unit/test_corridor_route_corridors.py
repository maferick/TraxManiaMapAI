"""Tests for the route_corridors persistence helpers.

Pure-function tests for path ranking and virtual-edge detection.
DB-touching build functions exercised via integration.
"""
from __future__ import annotations

import pytest

from src.corridor.traversability.route_corridors import (
    DEFAULT_TOP_N,
    _path_contains_virtual_edge,
    _rank_paths,
)


class TestRankPaths:
    def test_empty_returns_empty(self) -> None:
        assert _rank_paths([]) == []

    def test_shortest_first(self) -> None:
        a = [(0, 0, 0), (1, 0, 0), (2, 0, 0), (3, 0, 0)]  # length 4
        b = [(0, 0, 0), (1, 0, 0)]                         # length 2
        c = [(0, 0, 0), (1, 0, 0), (2, 0, 0)]              # length 3
        ranked = _rank_paths([a, b, c])
        assert ranked == [b, c, a]

    def test_ties_broken_lexicographically(self) -> None:
        # Same length — order by tuple content.
        p1 = [(0, 0, 0), (0, 1, 0)]
        p2 = [(0, 0, 0), (1, 0, 0)]
        ranked = _rank_paths([p2, p1])
        assert ranked == [p1, p2]

    def test_matches_top_ranked_path_semantics(self) -> None:
        # _rank_paths[0] must equal _top_ranked_path — same ordering
        # used by the §8.3.4 stability check.
        from src.corridor.traversability.enumeration import _top_ranked_path
        paths = [
            [(5, 0, 0), (5, 0, 1)],
            [(0, 0, 0), (1, 0, 0)],
            [(0, 0, 0), (1, 0, 0), (2, 0, 0)],
        ]
        top = _top_ranked_path(paths)
        ranked = _rank_paths(paths)
        assert tuple(tuple(c) for c in ranked[0]) == top


class TestPathContainsVirtualEdge:
    def test_empty_path_no_virtual(self) -> None:
        assert _path_contains_virtual_edge([], set()) is False

    def test_single_cell_path_no_virtual(self) -> None:
        # One-cell path has no edges at all.
        assert _path_contains_virtual_edge([(0, 0, 0)], set()) is False

    def test_detects_virtual_edge(self) -> None:
        virtual = {((0, 0, 0), (5, 0, 0))}
        path = [(0, 0, 0), (5, 0, 0)]
        assert _path_contains_virtual_edge(path, virtual) is True

    def test_no_false_positive_on_grid_only(self) -> None:
        # Grid path with no virtual edges.
        virtual: set[tuple[tuple[int, int, int], tuple[int, int, int]]] = set()
        path = [(0, 0, 0), (1, 0, 0), (2, 0, 0)]
        assert _path_contains_virtual_edge(path, virtual) is False

    def test_detects_virtual_in_middle(self) -> None:
        # Virtual edge mid-path — must still be flagged.
        virtual = {((1, 0, 0), (5, 0, 0))}
        path = [(0, 0, 0), (1, 0, 0), (5, 0, 0), (6, 0, 0)]
        assert _path_contains_virtual_edge(path, virtual) is True

    def test_pair_key_is_sorted(self) -> None:
        # Virtual edges are stored with sorted endpoints. A path
        # traversing the edge in either direction must match.
        virtual = {((0, 0, 0), (5, 0, 0))}
        path_forward = [(0, 0, 0), (5, 0, 0)]
        path_backward = [(5, 0, 0), (0, 0, 0)]
        assert _path_contains_virtual_edge(path_forward, virtual)
        assert _path_contains_virtual_edge(path_backward, virtual)


class TestDefaultTopN:
    def test_default_is_one_hundred(self) -> None:
        # The top-N default is load-bearing for schema sizing estimates.
        assert DEFAULT_TOP_N == 100
