"""Tests for the pure-compute side of the evidence persistence pipeline.

DB-touching functions (``build_map_evidence`` / ``build_set_evidence``)
are exercised via integration; unit tests here cover
``_build_rows_for_map`` — the logic that turns block_placements rows
into evidence tuples.
"""
from __future__ import annotations

import pytest

from src.corridor.traversability.classification import (
    CLASSIFICATION_VERSION,
    FamilyBucket,
)
from src.corridor.traversability.evidence import (
    DECO_CLUSTER_NEIGHBOR_THRESHOLD,
    _build_rows_for_map,
    _cell_to_placement_map,
    _count_non_drivable_neighbors,
    _normalize_pattern_weight,
)


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


class TestCellToPlacementMap:
    """The cell→placement mapping used by the path_support update must
    use the same promotion rule as _build_rows_for_map. If they drift,
    path_support writes would miss rows that DO exist in the evidence
    table (because the evidence table was built with one resolution
    but path_support lookup uses another)."""

    def test_single_placement_maps_directly(self) -> None:
        m = _cell_to_placement_map([_row(100, 0, 0, 0, "Platform")])
        assert m == {(0, 0, 0): 100}

    def test_layered_cell_uses_drivable_pid(self) -> None:
        # Same cell with Structure first (pid 100) and Road second
        # (pid 101). The map should resolve (0,0,0) → 101.
        m = _cell_to_placement_map([
            _row(100, 0, 0, 0, "Structure"),
            _row(101, 0, 0, 0, "Road"),
        ])
        assert m[(0, 0, 0)] == 101

    def test_layered_drivable_first_not_downgraded(self) -> None:
        # Road first, Structure second — should NOT switch to Structure.
        m = _cell_to_placement_map([
            _row(101, 0, 0, 0, "Road"),
            _row(100, 0, 0, 0, "Structure"),
        ])
        assert m[(0, 0, 0)] == 101

    def test_ambiguous_promotes_to_drivable(self) -> None:
        # Open (ambiguous) first, Road (drivable) second — promote.
        m = _cell_to_placement_map([
            _row(100, 0, 0, 0, "Open"),
            _row(101, 0, 0, 0, "Road"),
        ])
        assert m[(0, 0, 0)] == 101


class TestCountNonDrivableNeighbors:
    """§4 aggregates 6-axis neighbor counts per cell. A cell with no
    non-drivable neighbors scores 0 (minimum); fully deco-surrounded
    scores 6 (maximum)."""

    def test_no_neighbors_returns_zero(self) -> None:
        n = _count_non_drivable_neighbors((0, 0, 0), {})
        assert n == 0

    def test_all_six_deco_returns_six(self) -> None:
        buckets = {
            (1, 0, 0): FamilyBucket.NON_DRIVABLE,
            (-1, 0, 0): FamilyBucket.NON_DRIVABLE,
            (0, 1, 0): FamilyBucket.NON_DRIVABLE,
            (0, -1, 0): FamilyBucket.NON_DRIVABLE,
            (0, 0, 1): FamilyBucket.NON_DRIVABLE,
            (0, 0, -1): FamilyBucket.NON_DRIVABLE,
        }
        assert _count_non_drivable_neighbors((0, 0, 0), buckets) == 6

    def test_mixed_only_counts_non_drivable(self) -> None:
        buckets = {
            (1, 0, 0): FamilyBucket.NON_DRIVABLE,    # counts
            (-1, 0, 0): FamilyBucket.DRIVABLE,        # drivable — doesn't count
            (0, 1, 0): FamilyBucket.AMBIGUOUS,        # ambiguous — doesn't count
            (0, -1, 0): FamilyBucket.NON_DRIVABLE,    # counts
        }
        assert _count_non_drivable_neighbors((0, 0, 0), buckets) == 2

    def test_diagonal_neighbors_not_counted(self) -> None:
        # Only axis-6 are considered; diagonals ignored even if deco.
        buckets = {
            (1, 1, 0): FamilyBucket.NON_DRIVABLE,   # diagonal
            (1, 0, 1): FamilyBucket.NON_DRIVABLE,   # diagonal
        }
        assert _count_non_drivable_neighbors((0, 0, 0), buckets) == 0


class TestDecoClusterThreshold:
    def test_threshold_is_half_of_twelve(self) -> None:
        # 12 total possible axis-neighbors across both endpoints;
        # 6 = 50% of surrounding cells = "in a deco cluster."
        assert DECO_CLUSTER_NEIGHBOR_THRESHOLD == 6


class TestNormalizePatternWeight:
    """Signal-3 log-normalization rules."""

    def test_max_count_returns_one(self) -> None:
        assert _normalize_pattern_weight(100, 100) == pytest.approx(1.0, abs=1e-6)

    def test_zero_count_returns_zero(self) -> None:
        # log(0 + 1) = 0
        assert _normalize_pattern_weight(0, 100) == 0.0

    def test_max_count_zero_returns_zero(self) -> None:
        # Empty corpus fallback — no div-by-zero on log(1)
        assert _normalize_pattern_weight(0, 0) == 0.0
        assert _normalize_pattern_weight(0, -5) == 0.0

    def test_monotonic_in_count(self) -> None:
        # Higher count → higher weight for a fixed max
        w1 = _normalize_pattern_weight(10, 1_000_000)
        w2 = _normalize_pattern_weight(100, 1_000_000)
        w3 = _normalize_pattern_weight(1000, 1_000_000)
        assert w1 < w2 < w3
        assert 0 < w1 < 1
        assert 0 < w3 < 1

    def test_log_scale_differentiates_long_tail(self) -> None:
        # Count=10 vs count=1 should be meaningfully different even
        # when max_count is huge — that's the whole point of log scale.
        w1 = _normalize_pattern_weight(1, 5_000_000)
        w10 = _normalize_pattern_weight(10, 5_000_000)
        # log(2)/log(5M) ≈ 0.045; log(11)/log(5M) ≈ 0.156
        # Ratio should be meaningful, not near 1:1 like linear scale would give.
        assert w10 / w1 > 3

    def test_never_exceeds_one(self) -> None:
        # count == max_count → exactly 1.0; larger-than-max would go
        # above but is never the real input (we compute max from the
        # same distribution). Still check the math doesn't blow up.
        assert _normalize_pattern_weight(100, 100) <= 1.0 + 1e-9
