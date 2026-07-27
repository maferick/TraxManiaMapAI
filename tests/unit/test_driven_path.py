"""Tests for the driven-path Viterbi.

The properties under test are the ones that make the output usable as
training substrate: continuity (no invented teleports), honesty (holes
become OFF_SURFACE, never the nearest wrong block), and jump handling
(AIRBORNE between blocks rather than a forced ground transition).
"""
from __future__ import annotations

from src.catalogue.loader import BlockDef, BlockUnit, BlockVariant
from src.route.block_matcher import GROUND_ABS_Y_M, Placement
from src.route.driven_path import (
    AIRBORNE,
    OFF_SURFACE,
    extract_driven_path,
)


def _block(block_id, size=(1, 1, 1), offsets=((0, 0, 0),)):
    return BlockDef(
        block_id=block_id, name=block_id, page="", waypoint="None",
        is_pillar=False,
        variants=(BlockVariant(
            kind="ground", index=0, size=size,
            units=tuple(BlockUnit(offset=o, underground=False,
                                  terrain_modifier="", surface="", clips={})
                        for o in offsets)),),
        collection="Stadium2020",
    )


CAT = {"Road": _block("Road")}


def _row_of_blocks(n):
    """n Road blocks in a straight +z line at ground level."""
    return [
        Placement(index=i, block_type="Road", variant=0, is_free=False,
                  x=10, y=9, z=20 + i, rotation=0)
        for i in range(n)
    ]


def _sample(t, x, y, z, vy=0.0):
    return {"time_ms": t, "x": x, "y": y, "z": z,
            "vx": 0.0, "vy": vy, "vz": 10.0}


def _drive_z(z0, z1, *, t0=0, dt=50, step=4.0, y=GROUND_ABS_Y_M + 0.3):
    out = []
    z, t = z0, t0
    while z <= z1:
        out.append(_sample(t, 10 * 32 + 16, y, z))
        z += step
        t += dt
    return out


def test_straight_drive_yields_ordered_visits_without_teleports():
    placements = _row_of_blocks(4)
    samples = _drive_z(20 * 32 + 2, 24 * 32 - 2)
    path = extract_driven_path(samples, placements, CAT)

    block_states = [v.state for v in path.visits if v.state >= 0]
    assert block_states == [0, 1, 2, 3]
    assert path.stats["teleports"] == 0
    assert path.stats["off_surface_visits"] == 0


def test_hole_becomes_off_surface_not_nearest_block():
    # Blocks 0,1 then a one-cell hole, then block at z=23.
    placements = [
        Placement(0, "Road", 0, False, x=10, y=9, z=20),
        Placement(1, "Road", 0, False, x=10, y=9, z=21),
        Placement(2, "Road", 0, False, x=10, y=9, z=23),
    ]
    samples = _drive_z(20 * 32 + 2, 24 * 32 - 2)
    path = extract_driven_path(samples, placements, CAT)

    kinds = [v.state for v in path.visits]
    # The hole must appear as OFF_SURFACE between block visits; the
    # wrong alternative is stretching block 1 or 2 across the gap.
    assert OFF_SURFACE in kinds
    i = kinds.index(OFF_SURFACE)
    assert any(k >= 0 for k in kinds[:i])
    assert any(k >= 0 for k in kinds[i + 1:])


def test_ballistic_arc_is_airborne_between_blocks():
    placements = [
        Placement(0, "Road", 0, False, x=10, y=9, z=20),
        Placement(1, "Road", 0, False, x=10, y=9, z=24),
    ]
    samples = []
    t = 0
    # On block 0.
    for z in (642.0, 646.0, 650.0):
        samples.append(_sample(t, 336.0, 8.3, z))
        t += 50
    # Ballistic arc: vy decreasing at the measured -24 m/s2.
    vy = 6.0
    y = 8.3
    for z in (658.0, 666.0, 674.0, 682.0, 690.0, 698.0, 706.0, 714.0):
        samples.append(_sample(t, 336.0, y, z, vy=vy))
        y += vy * 0.05
        vy -= 24.0 * 0.05
        t += 50
    # Land on block 1.
    for z in (778.0, 782.0, 786.0):
        samples.append(_sample(t, 336.0, 8.3, z))
        t += 50

    path = extract_driven_path(samples, placements, CAT)
    kinds = [v.state for v in path.visits]
    assert kinds[0] == 0
    assert AIRBORNE in kinds
    assert kinds[-1] == 1
    # The jump is not a teleport: AIRBORNE bridges the two blocks.
    assert path.stats["teleports"] == 0


def test_swept_segment_does_not_skip_a_fast_traversal():
    # 40m per 50ms sample: without sweeping, the middle block of three
    # would fall between consecutive samples and vanish from the path.
    placements = _row_of_blocks(3)
    samples = [
        _sample(0, 336.0, 8.3, 20 * 32 + 4),
        _sample(50, 336.0, 8.3, 20 * 32 + 44),   # skips block 1's cell
        _sample(100, 336.0, 8.3, 20 * 32 + 84),
    ]
    path = extract_driven_path(samples, placements, CAT)
    visited = {v.state for v in path.visits if v.state >= 0}
    assert 1 in visited


def test_checkpoint_validation_counts_hits():
    placements = _row_of_blocks(3)
    samples = _drive_z(20 * 32 + 2, 23 * 32 - 2)
    mid = len(samples) // 2
    cell = (10, 9, 21)
    path = extract_driven_path(
        samples, placements, CAT,
        checkpoint_indices=[mid],
        waypoint_cells={cell: "Checkpoint"},
    )
    assert (path.checkpoint_hits, path.checkpoint_total) == (1, 1)
