"""Tests for the v0 geometry validity checker (task #226)."""
from __future__ import annotations

import pytest

from src.generation.geom_validator import (
    CODE_MISSING_SUPPORT,
    CODE_PARTIAL_MULTICELL,
    CODE_ROUTE_CELL_MISSING_BLOCK,
    CODE_ROUTE_GAP,
    CODE_SPAWN_INTERSECT,
    SEVERITY_FAIL,
    SEVERITY_WARN,
    GeometryInfo,
    _footprint_shadow_cells,
    validate_map_geometry,
)


def _grid_block(
    x: int, y: int, z: int, *,
    family: str = "Road", name: str = "RoadTechStraight",
    rotation: int = 0,
) -> dict:
    return {
        "placement": "grid",
        "x": x, "y": y, "z": z,
        "family": family, "name": name,
        "rotation": rotation,
    }


_UNIT = GeometryInfo(footprint_x=1, shape_class="straight")
_WALL4 = GeometryInfo(footprint_x=4, shape_class="straight",
                      connector_hint="straight_x")
_PLATFORM = GeometryInfo(footprint_x=1, shape_class="platform")
_START = GeometryInfo(footprint_x=1, shape_class="start", is_anchor_capable=True)


class TestFootprintShadowCells:
    """Pure-function rotation math — no Finding involvement."""

    def test_unit_footprint_returns_origin(self) -> None:
        assert _footprint_shadow_cells((5, 10, 7), 0, 1) == [(5, 10, 7)]

    def test_rotation_0_extends_plus_x(self) -> None:
        # A Straight4 at rot=0 occupies four cells along +X.
        cells = _footprint_shadow_cells((5, 10, 7), 0, 4)
        assert cells == [(5, 10, 7), (6, 10, 7), (7, 10, 7), (8, 10, 7)]

    def test_rotation_1_extends_plus_z(self) -> None:
        cells = _footprint_shadow_cells((5, 10, 7), 1, 4)
        assert cells == [(5, 10, 7), (5, 10, 8), (5, 10, 9), (5, 10, 10)]

    def test_rotation_2_extends_minus_x(self) -> None:
        cells = _footprint_shadow_cells((5, 10, 7), 2, 4)
        assert cells == [(5, 10, 7), (4, 10, 7), (3, 10, 7), (2, 10, 7)]

    def test_rotation_3_extends_minus_z(self) -> None:
        cells = _footprint_shadow_cells((5, 10, 7), 3, 4)
        assert cells == [(5, 10, 7), (5, 10, 6), (5, 10, 5), (5, 10, 4)]

    def test_rotation_wraps_on_overflow(self) -> None:
        # rot=5 → rot=1
        assert _footprint_shadow_cells((0, 0, 0), 5, 2) == [
            (0, 0, 0), (0, 0, 1),
        ]


class TestPartialMulticell:
    """Headline map-1212 failure: origin kept, shadow cell gone."""

    def test_empty_shadow_is_fail(self) -> None:
        # A Wall-Straight-4 at origin (5,10,7), rot=0, with nothing
        # at (6,10,7), (7,10,7), (8,10,7) — three failures, one per
        # missing shadow cell.
        lookup = {("Platform", "PlatformPlasticWallStraight4"): _WALL4}
        blocks = [_grid_block(
            5, 10, 7, family="Platform",
            name="PlatformPlasticWallStraight4", rotation=0,
        )]
        report = validate_map_geometry(
            blocks=blocks, geometry_lookup=lookup,
        )
        fails = [
            f for f in report.findings
            if f.code == CODE_PARTIAL_MULTICELL and f.severity == SEVERITY_FAIL
        ]
        assert len(fails) == 3
        assert {f.cell for f in fails} == {(6, 10, 7), (7, 10, 7), (8, 10, 7)}
        assert report.has_failures is True

    def test_complete_footprint_has_no_findings(self) -> None:
        # Same Wall4 at rot=0, but the three shadow cells are backed
        # by (here) deco unit-footprint blocks — mesh is whole.
        lookup = {
            ("Platform", "PlatformPlasticWallStraight4"): _WALL4,
            ("Deco", "Filler"): _UNIT,
        }
        blocks = [
            _grid_block(5, 10, 7, family="Platform",
                        name="PlatformPlasticWallStraight4", rotation=0),
            _grid_block(6, 10, 7, family="Deco", name="Filler"),
            _grid_block(7, 10, 7, family="Deco", name="Filler"),
            _grid_block(8, 10, 7, family="Deco", name="Filler"),
        ]
        report = validate_map_geometry(
            blocks=blocks, geometry_lookup=lookup,
        )
        # Shadow cells are occupied by other blocks → warn, not fail.
        # The warnings are useful diagnostic signal even if the map
        # visually reads as whole.
        assert not report.has_failures
        warns = [
            f for f in report.findings
            if f.code == CODE_PARTIAL_MULTICELL and f.severity == SEVERITY_WARN
        ]
        assert len(warns) == 3

    def test_unknown_block_is_not_a_multicell_finding(self) -> None:
        # No geometry entry → we can't know footprint, so partial-
        # multicell check can't fire. (unknown_block *could* be its
        # own finding later; v0 just skips.)
        blocks = [_grid_block(0, 0, 0, family="X", name="Y")]
        report = validate_map_geometry(
            blocks=blocks, geometry_lookup={},
        )
        assert report.by_code(CODE_PARTIAL_MULTICELL) == []


class TestRouteContinuity:
    def test_chebyshev_step_1_is_clean(self) -> None:
        route = [(0, 10, 0), (1, 10, 0), (1, 10, 1), (2, 11, 1)]
        report = validate_map_geometry(
            blocks=[_grid_block(x, y, z) for x, y, z in route],
            geometry_lookup={("Road", "RoadTechStraight"): _UNIT},
            route_cells=route,
        )
        assert report.by_code(CODE_ROUTE_GAP) == []

    def test_large_step_warns(self) -> None:
        # cheb=3 between (0,10,0) and (3,10,0).
        route = [(0, 10, 0), (3, 10, 0)]
        # Put something at the endpoints so route_cell_missing_block
        # doesn't contaminate the assertion.
        report = validate_map_geometry(
            blocks=[_grid_block(0, 10, 0), _grid_block(3, 10, 0)],
            geometry_lookup={("Road", "RoadTechStraight"): _UNIT},
            route_cells=route,
            max_route_step_cheb=1,
        )
        gaps = report.by_code(CODE_ROUTE_GAP)
        assert len(gaps) == 1
        assert gaps[0].severity == SEVERITY_WARN
        assert gaps[0].cell == (3, 10, 0)


class TestRouteCellsHaveBlocks:
    def test_empty_route_cell_is_fail(self) -> None:
        # Route crosses (1,10,0) but no block is placed there.
        route = [(0, 10, 0), (1, 10, 0), (2, 10, 0)]
        report = validate_map_geometry(
            blocks=[_grid_block(0, 10, 0), _grid_block(2, 10, 0)],
            geometry_lookup={("Road", "RoadTechStraight"): _UNIT},
            route_cells=route,
        )
        missing = report.by_code(CODE_ROUTE_CELL_MISSING_BLOCK)
        assert len(missing) == 1
        assert missing[0].severity == SEVERITY_FAIL
        assert missing[0].cell == (1, 10, 0)


class TestMissingSupport:
    def test_elevated_non_self_supporting_warns(self) -> None:
        # Ground at y=9; route cell at y=12 with a straight (not
        # self-supporting) and nothing at y=11 → warn.
        route = [(5, 12, 3)]
        blocks = [_grid_block(5, 12, 3)]  # Road/Straight, shape=straight
        report = validate_map_geometry(
            blocks=blocks,
            geometry_lookup={("Road", "RoadTechStraight"): _UNIT},
            route_cells=route,
            ground_y=9,
        )
        warns = report.by_code(CODE_MISSING_SUPPORT)
        assert len(warns) == 1
        assert warns[0].cell == (5, 12, 3)

    def test_self_supporting_shape_skipped(self) -> None:
        route = [(5, 12, 3)]
        blocks = [_grid_block(
            5, 12, 3, family="Platform",
            name="PlatformPlasticGenericPlatform",
        )]
        report = validate_map_geometry(
            blocks=blocks,
            geometry_lookup={
                ("Platform", "PlatformPlasticGenericPlatform"): _PLATFORM,
            },
            route_cells=route,
            ground_y=9,
        )
        assert report.by_code(CODE_MISSING_SUPPORT) == []

    def test_ground_level_cell_not_flagged(self) -> None:
        # Route cell at the ground Y — no support expected.
        route = [(5, 9, 3)]
        blocks = [_grid_block(5, 9, 3)]
        report = validate_map_geometry(
            blocks=blocks,
            geometry_lookup={("Road", "RoadTechStraight"): _UNIT},
            route_cells=route,
            ground_y=9,
        )
        assert report.by_code(CODE_MISSING_SUPPORT) == []


class TestSpawnIntersect:
    def test_non_anchor_block_on_spawn_fails(self) -> None:
        spawn = (5, 10, 5)
        blocks = [_grid_block(5, 10, 5)]  # straight, not a start
        report = validate_map_geometry(
            blocks=blocks,
            geometry_lookup={("Road", "RoadTechStraight"): _UNIT},
            spawn_cell=spawn,
        )
        fails = [
            f for f in report.by_code(CODE_SPAWN_INTERSECT)
            if f.cell == spawn
        ]
        assert len(fails) == 1
        assert fails[0].severity == SEVERITY_FAIL

    def test_headroom_block_fails(self) -> None:
        spawn = (5, 10, 5)
        blocks = [
            _grid_block(5, 10, 5, family="Platform",
                        name="PlatformPlasticStart"),   # correct anchor
            _grid_block(5, 11, 5),                       # headroom block
        ]
        report = validate_map_geometry(
            blocks=blocks,
            geometry_lookup={
                ("Platform", "PlatformPlasticStart"): _START,
                ("Road", "RoadTechStraight"): _UNIT,
            },
            spawn_cell=spawn,
        )
        fails = [
            f for f in report.by_code(CODE_SPAWN_INTERSECT)
            if f.cell == (5, 11, 5)
        ]
        assert len(fails) == 1

    def test_anchor_spawn_clear_headroom_is_clean(self) -> None:
        spawn = (5, 10, 5)
        blocks = [_grid_block(
            5, 10, 5, family="Platform", name="PlatformPlasticStart",
        )]
        report = validate_map_geometry(
            blocks=blocks,
            geometry_lookup={("Platform", "PlatformPlasticStart"): _START},
            spawn_cell=spawn,
        )
        assert report.by_code(CODE_SPAWN_INTERSECT) == []


class TestReportShape:
    def test_empty_map_has_no_findings(self) -> None:
        report = validate_map_geometry(
            blocks=[], geometry_lookup={},
        )
        assert report.findings == []
        assert report.has_failures is False
        assert report.blocks_total == 0

    def test_checks_run_is_populated(self) -> None:
        report = validate_map_geometry(
            blocks=[_grid_block(0, 9, 0)],
            geometry_lookup={("Road", "RoadTechStraight"): _UNIT},
            route_cells=[(0, 9, 0)],
            spawn_cell=(0, 9, 0),
        )
        assert CODE_PARTIAL_MULTICELL in report.checks_run
        assert CODE_ROUTE_GAP in report.checks_run
        assert CODE_ROUTE_CELL_MISSING_BLOCK in report.checks_run
        assert CODE_MISSING_SUPPORT in report.checks_run
        assert CODE_SPAWN_INTERSECT in report.checks_run

    def test_skipped_checks_not_in_checks_run(self) -> None:
        # No route, no spawn → only the multicell self-check runs.
        report = validate_map_geometry(
            blocks=[_grid_block(0, 9, 0)],
            geometry_lookup={("Road", "RoadTechStraight"): _UNIT},
        )
        assert report.checks_run == [CODE_PARTIAL_MULTICELL]


class TestMap1212Reproduction:
    """Reproduce the diagnostic-identified failure shape on map 1212.

    PlatformPlasticWallStraight4 at cell (31, 13, 22) rot=0; the strip
    kept the origin but the prism halo didn't preserve (32,13,22),
    (33,13,22), (34,13,22). Expect three partial_multicell failures.
    """

    def test_reproduction(self) -> None:
        lookup = {("Platform", "PlatformPlasticWallStraight4"): _WALL4}
        blocks = [_grid_block(
            31, 13, 22, family="Platform",
            name="PlatformPlasticWallStraight4", rotation=0,
        )]
        report = validate_map_geometry(
            blocks=blocks, geometry_lookup=lookup,
        )
        fails = [
            f for f in report.by_code(CODE_PARTIAL_MULTICELL)
            if f.severity == SEVERITY_FAIL
        ]
        assert {f.cell for f in fails} == {
            (32, 13, 22), (33, 13, 22), (34, 13, 22),
        }
        assert report.has_failures is True
