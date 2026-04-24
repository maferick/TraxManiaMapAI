"""Unit tests for Level-2 strip-to-route (src.generation.stripper)."""
from __future__ import annotations

from typing import Any

import pytest

from src.generation import (
    Anchor,
    AssembledRoute,
    ChosenCorridor,
    IntervalAssembly,
)
from src.generation.stripper import (
    STRIP_POLICY_HALO_AXIS_1,
    STRIP_POLICY_NONE,
    StripResult,
    compute_kept_cells,
    filter_blocks_by_cells,
    strip_route,
    verify_route_on_kept_cells,
)


# ---------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------

def _corridor(
    *, corridor_id: int, src: Anchor, dst: Anchor,
    cells: tuple[tuple[int, int, int], ...], score: float = 0.7,
) -> ChosenCorridor:
    return ChosenCorridor(
        corridor_id=corridor_id, map_id=1, src=src, dst=dst,
        path_cells=cells, path_length=len(cells),
        contains_virtual_edge=False,
        corridor_confidence=0.5, learned_corridor_score=score,
        expected_time_ms=1000,
    )


def _route(intervals: list[IntervalAssembly]) -> AssembledRoute:
    anchors = [intervals[0].src]
    for iv in intervals:
        anchors.append(iv.dst)
    return AssembledRoute(
        map_id=1, anchors=tuple(anchors), intervals=tuple(intervals),
        cells_total=sum(iv.chosen.path_length for iv in intervals),
        estimated_time_ms=sum(iv.chosen.expected_time_ms for iv in intervals),
        ai_confidence=0.7,
    )


def _block(x: int, y: int, z: int, *, fam: str = "Road", name: str = "R1") -> dict[str, Any]:
    return {
        "block_family": fam, "block_name": name,
        "x": x, "y": y, "z": z, "rotation": 0,
    }


# ---------------------------------------------------------------------
# compute_kept_cells — halo shape + anchor inclusion
# ---------------------------------------------------------------------

class TestComputeKeptCells:
    def test_halo_axis_1_is_seven_cells(self) -> None:
        # Single-cell path → cell itself + 6 axis neighbours.
        spawn = Anchor("Spawn", 0, (0, 0, 0))
        goal = Anchor("Goal", 0, (1, 0, 0))
        iv = IntervalAssembly(
            index=0, src=spawn, dst=goal,
            chosen=_corridor(
                corridor_id=1, src=spawn, dst=goal,
                cells=((5, 5, 5),),
            ),
        )
        route = _route([iv])
        kept = compute_kept_cells(route, policy=STRIP_POLICY_HALO_AXIS_1)
        expected = {
            (5, 5, 5),
            (6, 5, 5), (4, 5, 5),
            (5, 6, 5), (5, 4, 5),
            (5, 5, 6), (5, 5, 4),
        }
        # Also includes spawn (0,0,0) + goal (1,0,0) via anchor cells.
        assert expected <= kept
        assert (0, 0, 0) in kept
        assert (1, 0, 0) in kept

    def test_halo_axis_1_excludes_diagonals(self) -> None:
        # Diagonal cells must NOT be in the halo.
        spawn = Anchor("Spawn", 0, (0, 0, 0))
        goal = Anchor("Goal", 0, (10, 10, 10))
        iv = IntervalAssembly(
            index=0, src=spawn, dst=goal,
            chosen=_corridor(
                corridor_id=1, src=spawn, dst=goal,
                cells=((5, 5, 5),),
            ),
        )
        kept = compute_kept_cells(_route([iv]))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    if abs(dx) + abs(dy) + abs(dz) > 1:
                        # 2- or 3-axis diagonal → must NOT be kept.
                        assert (5 + dx, 5 + dy, 5 + dz) not in kept, (dx, dy, dz)

    def test_anchor_cells_kept_even_if_not_on_path(self) -> None:
        # A multi-cell CP (represented here by a Goal anchor whose
        # cell is away from any chosen path_cell) must survive.
        spawn = Anchor("Spawn", 0, (0, 0, 0))
        goal = Anchor("Goal", 0, (99, 99, 99))  # nowhere near path
        iv = IntervalAssembly(
            index=0, src=spawn, dst=goal,
            chosen=_corridor(
                corridor_id=1, src=spawn, dst=goal,
                cells=((0, 0, 0), (1, 0, 0), (2, 0, 0)),
            ),
        )
        kept = compute_kept_cells(_route([iv]))
        assert (99, 99, 99) in kept


# ---------------------------------------------------------------------
# filter_blocks_by_cells — drop / keep rules
# ---------------------------------------------------------------------

class TestFilterBlocksByCells:
    def test_keeps_only_blocks_whose_cell_is_in_kept(self) -> None:
        blocks = [_block(0, 0, 0), _block(1, 0, 0), _block(100, 0, 0)]
        kept_cells = frozenset({(0, 0, 0), (1, 0, 0)})
        out = filter_blocks_by_cells(blocks, kept_cells)
        assert len(out) == 2
        assert {(b["x"], b["y"], b["z"]) for b in out} == kept_cells

    def test_drops_blocks_without_coords(self) -> None:
        # Free-placed blocks (NULL grid coords) always dropped —
        # scope-v0 doesn't carry free blocks today but this guards
        # against future schema revs.
        blocks = [
            {"block_family": "X", "block_name": "x",
             "x": None, "y": None, "z": None, "rotation": 0},
            _block(1, 1, 1),
        ]
        kept = frozenset({(1, 1, 1)})
        out = filter_blocks_by_cells(blocks, kept)
        assert len(out) == 1
        assert out[0]["x"] == 1

    def test_empty_kept_drops_everything(self) -> None:
        out = filter_blocks_by_cells([_block(0, 0, 0)], frozenset())
        assert out == []


# ---------------------------------------------------------------------
# verify_route_on_kept_cells
# ---------------------------------------------------------------------

class TestVerifyRouteOnKeptCells:
    def test_happy_all_cells_kept(self) -> None:
        spawn = Anchor("Spawn", 0, (0, 0, 0))
        goal = Anchor("Goal", 0, (2, 0, 0))
        iv = IntervalAssembly(
            index=0, src=spawn, dst=goal,
            chosen=_corridor(
                corridor_id=1, src=spawn, dst=goal,
                cells=((0, 0, 0), (1, 0, 0), (2, 0, 0)),
            ),
        )
        kept = frozenset({(0, 0, 0), (1, 0, 0), (2, 0, 0)})
        ok, detail = verify_route_on_kept_cells(_route([iv]), kept)
        assert ok
        assert detail is None

    def test_broken_when_path_cell_missing(self) -> None:
        spawn = Anchor("Spawn", 0, (0, 0, 0))
        goal = Anchor("Goal", 0, (2, 0, 0))
        iv = IntervalAssembly(
            index=0, src=spawn, dst=goal,
            chosen=_corridor(
                corridor_id=42, src=spawn, dst=goal,
                cells=((0, 0, 0), (1, 0, 0), (2, 0, 0)),
            ),
        )
        # Halo "too tight" — (1,0,0) got dropped.
        kept = frozenset({(0, 0, 0), (2, 0, 0)})
        ok, detail = verify_route_on_kept_cells(_route([iv]), kept)
        assert not ok
        assert detail and "42" in detail and "(1, 0, 0)" in detail


# ---------------------------------------------------------------------
# strip_route — end-to-end
# ---------------------------------------------------------------------

class TestStripRoute:
    def _sample(self) -> tuple[AssembledRoute, list[dict[str, Any]]]:
        spawn = Anchor("Spawn", 0, (0, 0, 0))
        cp = Anchor("LinkedCheckpoint", 1, (3, 0, 0))
        goal = Anchor("Goal", 0, (5, 0, 0))
        c1 = _corridor(
            corridor_id=1, src=spawn, dst=cp,
            cells=((0, 0, 0), (1, 0, 0), (2, 0, 0), (3, 0, 0)),
        )
        c2 = _corridor(
            corridor_id=2, src=cp, dst=goal,
            cells=((3, 0, 0), (4, 0, 0), (5, 0, 0)),
        )
        route = _route([
            IntervalAssembly(index=0, src=spawn, dst=cp, chosen=c1),
            IntervalAssembly(index=1, src=cp, dst=goal, chosen=c2),
        ])
        # Base has route blocks + a few irrelevant ones far away.
        blocks = [_block(x, 0, 0) for x in range(6)] + [
            _block(50, 50, 50), _block(-10, 0, 0),
        ]
        return route, blocks

    def test_halo_axis_1_keeps_route_and_halo_drops_far(self) -> None:
        route, blocks = self._sample()
        result = strip_route(route, blocks)
        assert result.strip_policy == STRIP_POLICY_HALO_AXIS_1
        assert result.route_intact is True
        assert result.broken_detail is None
        # Route cells (0..5, 0, 0) all kept; (50,50,50) and (-10,0,0)
        # are outside the axis-1 halo → dropped.
        kept_cells = {(b["x"], b["y"], b["z"]) for b in result.stripped_blocks}
        for x in range(6):
            assert (x, 0, 0) in kept_cells
        assert (50, 50, 50) not in kept_cells
        assert (-10, 0, 0) not in kept_cells
        assert result.base_block_count == len(blocks)
        assert result.kept_block_count == len(result.stripped_blocks)

    def test_policy_none_is_passthrough(self) -> None:
        route, blocks = self._sample()
        result = strip_route(route, blocks, policy=STRIP_POLICY_NONE)
        assert result.stripped_blocks == blocks
        assert result.kept_block_count == len(blocks)
        assert result.route_intact is True

    def test_unknown_policy_rejected(self) -> None:
        route, blocks = self._sample()
        with pytest.raises(ValueError, match="unknown strip policy"):
            strip_route(route, blocks, policy="mystery_halo")

    def test_result_has_expected_strip_policy(self) -> None:
        route, blocks = self._sample()
        result = strip_route(route, blocks)
        assert isinstance(result, StripResult)
        assert result.strip_policy == STRIP_POLICY_HALO_AXIS_1
