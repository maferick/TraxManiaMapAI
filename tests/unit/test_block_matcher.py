"""Tests for telemetry-to-block candidate generation.

The properties that matter here are asymmetric: over-generating a
candidate costs a little Viterbi work, but dropping the true block makes
it unrecoverable. So the tests check that fallbacks widen rather than
narrow, and that the airborne test never consults block data.
"""
from __future__ import annotations

import math

from src.catalogue.loader import BlockDef, BlockUnit, BlockVariant
from src.route.block_matcher import (
    CandidateIndex,
    GROUND_ABS_Y_M,
    GROUND_ROW,
    Placement,
    classify_airborne,
    free_placement_box,
    grid_footprint_cells,
    to_cell,
)


def _unit(offset):
    return BlockUnit(offset=offset, underground=False, terrain_modifier="",
                     surface="", clips={})


def _block(block_id, size, offsets, kind="ground"):
    return BlockDef(
        block_id=block_id, name=block_id, page="", waypoint="None",
        is_pillar=False,
        variants=(BlockVariant(kind=kind, index=0, size=size,
                               units=tuple(_unit(o) for o in offsets)),),
        collection="Stadium2020",
    )


def test_ground_row_maps_to_row_nine():
    # The calibration anchor: absolute y=8m is grid row 9.
    assert to_cell(0.0, GROUND_ABS_Y_M, 0.0)[1] == GROUND_ROW
    assert to_cell(0.0, GROUND_ABS_Y_M + 0.4, 0.0)[1] == GROUND_ROW
    # One level up is 8 metres.
    assert to_cell(0.0, GROUND_ABS_Y_M + 8.0, 0.0)[1] == GROUND_ROW + 1


def test_cell_is_32m_square_in_xz():
    assert to_cell(31.9, 8.0, 31.9)[0::2] == (0, 0)
    assert to_cell(32.1, 8.0, 32.1)[0::2] == (1, 1)


def test_footprint_uses_the_mask_not_just_the_anchor():
    cat = {"Long": _block("Long", (1, 1, 2), [(0, 0, 0), (0, 0, 1)])}
    p = Placement(index=0, block_type="Long", variant=0, is_free=False,
                  x=5, y=9, z=7, rotation=0)
    cells = grid_footprint_cells(p, cat)
    # Anchor-only indexing would return just (5,9,7) and lose the far
    # cell, which is exactly the residual this module exists to fix.
    assert set(cells) == {(5, 9, 7), (5, 9, 8)}


def test_footprint_rotates_with_the_placement():
    cat = {"Long": _block("Long", (1, 1, 2), [(0, 0, 0), (0, 0, 1)])}
    straight = grid_footprint_cells(
        Placement(0, "Long", 0, False, x=0, y=9, z=0, rotation=0), cat)
    turned = grid_footprint_cells(
        Placement(0, "Long", 0, False, x=0, y=9, z=0, rotation=1), cat)
    assert set(straight) == {(0, 9, 0), (0, 9, 1)}
    # A quarter turn puts the second cell on the X axis instead of Z.
    assert {c[2] for c in turned} == {0}
    assert len({c[0] for c in turned}) == 2


def test_unknown_block_still_generates_a_candidate():
    p = Placement(0, "SomeCustomBlock", 0, False, x=3, y=9, z=4, rotation=2)
    assert grid_footprint_cells(p, {}) == ((3, 9, 4),)


def test_free_placement_is_not_grid_matched_and_yields_a_volume():
    cat = {"Thing": _block("Thing", (1, 1, 1), [(0, 0, 0)])}
    p = Placement(0, "Thing", 0, True, abs_x=100.0, abs_y=50.0,
                  abs_z=200.0, yaw=0.0)
    assert grid_footprint_cells(p, cat) == ()
    box = free_placement_box(p, cat, anchor="center")
    assert box is not None
    assert box.contains(100.0, 50.0, 200.0)
    # 1x1x1 cell is 32m square, 8m tall: half-extents 16/4/16.
    assert box.contains(115.0, 50.0, 200.0)
    assert not box.contains(150.0, 50.0, 200.0)


def test_free_volume_respects_yaw():
    cat = {"Bar": _block("Bar", (1, 1, 3), [(0, 0, 0), (0, 0, 1), (0, 0, 2)])}
    p = Placement(0, "Bar", 0, True, abs_x=0.0, abs_y=0.0, abs_z=0.0,
                  yaw=math.pi / 2)
    box = free_placement_box(p, cat, anchor="center")
    # Unrotated the long axis is Z (3 cells = 96m, half 48). Rotated a
    # quarter turn it must reach along X instead.
    assert box.contains(40.0, 0.0, 0.0)
    assert not box.contains(0.0, 0.0, 40.0)


def test_index_separates_grid_and_free_counts():
    cat = {"A": _block("A", (1, 1, 1), [(0, 0, 0)])}
    idx = CandidateIndex([
        Placement(0, "A", 0, False, x=1, y=9, z=1),
        Placement(1, "A", 0, True, abs_x=500.0, abs_y=8.0, abs_z=500.0),
    ], cat)
    assert (idx.n_grid, idx.n_free) == (1, 1)
    assert idx.grid_hit(32.0 * 1 + 5, GROUND_ABS_Y_M, 32.0 * 1 + 5)
    assert idx.free_hit(500.0, 8.0, 500.0)


def test_airborne_is_decided_by_kinematics_alone():
    # Free fall at the measured TM2020 gravity, no block data in sight.
    samples = []
    vy = 20.0
    for i in range(10):
        samples.append({"time_ms": i * 50, "vy": vy})
        vy += -24.0 * 0.05
    flags = classify_airborne(samples)
    assert all(flags[1:-1])

    # A car held on a surface is not airborne.
    flat = [{"time_ms": i * 50, "vy": 0.0} for i in range(10)]
    assert not any(classify_airborne(flat))


def test_airborne_ignores_a_steep_descent():
    # Driving down a slope loses height steadily but is NOT ballistic;
    # counting it as airborne would excuse real coverage failures.
    samples = [{"time_ms": i * 50, "vy": -8.0} for i in range(10)]
    assert not any(classify_airborne(samples))
