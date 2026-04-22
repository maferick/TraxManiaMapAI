"""Tests for the pure-compute side of the evidence persistence pipeline.

DB-touching functions (``build_map_evidence`` / ``build_set_evidence``)
are exercised via integration; unit tests here cover
``_build_rows_for_map`` — the logic that turns block_placements rows
into evidence tuples.
"""
from __future__ import annotations

import pytest

from src.corridor.traversability.classification import CLASSIFICATION_VERSION
from src.corridor.traversability.evidence import _build_rows_for_map


def _row(pid: int, x: int, y: int, z: int, fam: str) -> tuple[int, int, int, int, str]:
    return (pid, x, y, z, fam)


class TestBuildRowsForMap:
    def test_empty_placements_yields_no_rows(self) -> None:
        rows, counts = _build_rows_for_map([], CLASSIFICATION_VERSION, map_id=1)
        assert rows == []
        assert counts == {"seed_valid": 0, "unsupported": 0, "unknown": 0}

    def test_single_placement_yields_no_rows(self) -> None:
        # One isolated block — no axis neighbors, no edges.
        rows, counts = _build_rows_for_map(
            [_row(100, 0, 0, 0, "Platform")],
            CLASSIFICATION_VERSION, map_id=1,
        )
        assert rows == []

    def test_drivable_pair_emits_seed_valid(self) -> None:
        rows, counts = _build_rows_for_map(
            [_row(100, 0, 0, 0, "Platform"), _row(101, 1, 0, 0, "Road")],
            CLASSIFICATION_VERSION, map_id=7,
        )
        assert len(rows) == 1
        (map_id, lo, hi, state, rule_support, version) = rows[0]
        assert map_id == 7
        assert {lo, hi} == {100, 101}
        assert lo < hi
        assert state == "seed_valid"
        assert rule_support == 1
        assert version == CLASSIFICATION_VERSION
        assert counts["seed_valid"] == 1

    def test_drivable_to_deco_emits_unsupported(self) -> None:
        rows, counts = _build_rows_for_map(
            [_row(100, 0, 0, 0, "Platform"), _row(101, 1, 0, 0, "Deco")],
            CLASSIFICATION_VERSION, map_id=1,
        )
        assert len(rows) == 1
        state = rows[0][3]
        rule_support = rows[0][4]
        assert state == "unsupported"
        assert rule_support == 0
        assert counts["unsupported"] == 1

    def test_drivable_to_ambiguous_emits_unknown(self) -> None:
        rows, counts = _build_rows_for_map(
            [_row(100, 0, 0, 0, "Platform"), _row(101, 1, 0, 0, "Open")],
            CLASSIFICATION_VERSION, map_id=1,
        )
        assert counts["unknown"] == 1
        assert rows[0][3] == "unknown"

    def test_layered_cell_prefers_drivable(self) -> None:
        # A Structure block and a Road block at the SAME cell.
        # Priority promotion picks Road; the adjacent Platform block
        # should form a seed_valid edge with the Road-resolved cell.
        rows, counts = _build_rows_for_map(
            [
                _row(100, 0, 0, 0, "Structure"),
                _row(101, 0, 0, 0, "Road"),       # same cell, drivable
                _row(102, 1, 0, 0, "Platform"),
            ],
            CLASSIFICATION_VERSION, map_id=1,
        )
        # One edge total: (0,0,0) ↔ (1,0,0). Resolved family of
        # (0,0,0) is Road (promoted over Structure) → seed_valid.
        assert len(rows) == 1
        assert rows[0][3] == "seed_valid"
        # The resolved placement_id for (0,0,0) is the one the
        # promotion picked — Road's pid=101.
        assert 101 in (rows[0][1], rows[0][2])
        assert 102 in (rows[0][1], rows[0][2])

    def test_deduped_axis_edges(self) -> None:
        # A→B adjacency appears via two symmetric iterations; rows
        # dedupe on ordered (lo, hi) pair.
        rows, counts = _build_rows_for_map(
            [_row(100, 0, 0, 0, "Platform"), _row(101, 1, 0, 0, "Road")],
            CLASSIFICATION_VERSION, map_id=1,
        )
        assert len(rows) == 1

    def test_all_six_axis_neighbors(self) -> None:
        placements = [_row(100, 0, 0, 0, "Platform")]
        for i, off in enumerate([
            (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1),
        ]):
            placements.append(_row(101 + i, *off, "Platform"))
        rows, counts = _build_rows_for_map(
            placements, CLASSIFICATION_VERSION, map_id=1,
        )
        # 6 axis neighbors → 6 seed_valid edges.
        assert len(rows) == 6
        assert counts["seed_valid"] == 6

    def test_diagonal_neighbors_are_not_counted(self) -> None:
        rows, counts = _build_rows_for_map(
            [
                _row(100, 0, 0, 0, "Platform"),
                _row(101, 1, 1, 0, "Platform"),    # diagonal
                _row(102, 1, 0, 1, "Platform"),    # diagonal
            ],
            CLASSIFICATION_VERSION, map_id=1,
        )
        # Zero axis edges — only diagonal pairs present.
        assert rows == []

    def test_row_ordering_stable_within_pair(self) -> None:
        # Whether (100, 101) or (101, 100) is emitted, lo < hi always.
        rows, _ = _build_rows_for_map(
            [_row(250, 0, 0, 0, "Platform"), _row(200, 1, 0, 0, "Road")],
            CLASSIFICATION_VERSION, map_id=1,
        )
        lo, hi = rows[0][1], rows[0][2]
        assert lo < hi
        assert {lo, hi} == {200, 250}

    def test_self_loop_skipped(self) -> None:
        # Hypothetical: same placement_id appears as both endpoints
        # (would only happen on malformed input). Should be dropped.
        rows, _ = _build_rows_for_map(
            [
                _row(100, 0, 0, 0, "Platform"),
                # A second row at the same cell with the SAME placement_id
                # is nonsensical but defend against it.
                _row(100, 1, 0, 0, "Road"),
            ],
            CLASSIFICATION_VERSION, map_id=1,
        )
        # Two cells (0,0,0)=100 and (1,0,0)=100. An edge (100, 100)
        # collapses to self-loop → skipped.
        assert rows == []
