"""Unit tests for src.generation.clip_walker.

The synthetic catalogue mirrors the real RoadTech clip topology
(start exits north-only, straight is n/s, curve is n/e, finish
enters south-only) so walker invariants are exercised without the
15 MB real catalogue. A smoke test against the real catalogue runs
when data/catalogue/catalogue.ndjson exists.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.catalogue.loader import FACE_DELTAS, load_catalogue, opposite_face
from src.generation.clip_walker import ClipWalker, Placement, RouteDeadEnd

REAL_CATALOGUE = Path("data/catalogue/catalogue.ndjson")

CLIP = "RoadTechFC"


def _block(block_id: str, waypoint: str, clips: dict[str, list[str]]) -> dict:
    return {
        "type": "block",
        "id": block_id,
        "name": block_id,
        "page": "RoadTech/Main/",
        "waypoint": waypoint,
        "is_pillar": False,
        "variants": [
            {
                "kind": "ground",
                "index": 0,
                "size": [1, 1, 1],
                "units": [
                    {
                        "offset": [0, 0, 0],
                        "underground": False,
                        "terrain_modifier": "",
                        "surface": "",
                        "clips": clips,
                    }
                ],
            }
        ],
    }


def _multiblock(block_id: str, waypoint: str, size, units) -> dict:
    """Multi-cell block: units = [(offset, clips), ...]."""
    return {
        "type": "block",
        "id": block_id,
        "name": block_id,
        "page": "RoadTech/Main/",
        "waypoint": waypoint,
        "is_pillar": False,
        "variants": [
            {
                "kind": "ground",
                "index": 0,
                "size": list(size),
                "units": [
                    {
                        "offset": list(offset),
                        "underground": False,
                        "terrain_modifier": "",
                        "surface": "",
                        "clips": clips,
                    }
                    for offset, clips in units
                ],
            }
        ],
    }


@pytest.fixture()
def synthetic_catalogue(tmp_path):
    records = [
        _block("Start", "Start", {"n": [CLIP]}),
        _block("Finish", "Finish", {"s": [CLIP]}),
        _block("Checkpoint", "Checkpoint", {"n": [CLIP], "s": [CLIP]}),
        _block("Straight", "None", {"n": [CLIP], "s": [CLIP]}),
        _block("Curve", "None", {"n": [CLIP], "e": [CLIP]}),
        # Mirrors real RoadTechCurve2: 2x2 footprint, ports on
        # opposite corners.
        _multiblock("Curve2", "None", (2, 1, 2), [
            ((0, 0, 0), {"e": [CLIP]}),
            ((1, 0, 0), {}),
            ((1, 0, 1), {"n": [CLIP]}),
            ((0, 0, 1), {}),
        ]),
        # Mirrors real RoadTechSlopeBase: 1x2x1, flat road clips at
        # both ends one cell apart vertically, plus a WALL clip that
        # the route-clip allowlist must ignore.
        _multiblock("SlopeUp", "None", (1, 2, 1), [
            ((0, 0, 0), {"s": [CLIP], "n": ["TrackWallVFC"]}),
            ((0, 1, 0), {"n": [CLIP]}),
        ]),
    ]
    path = tmp_path / "catalogue.ndjson"
    lines = [json.dumps({"type": "meta", "schema": "block_catalogue_v1"})]
    lines += [json.dumps(r) for r in records]
    path.write_text("\n".join(lines), encoding="utf-8")
    (tmp_path / "catalogue.done.json").write_text("{}", encoding="utf-8")
    return load_catalogue(path)


def _assert_route_closed(placements: list[Placement]) -> None:
    """Consecutive placements must be grid-adjacent and non-overlapping."""
    cells = [(p.x, p.z) for p in placements]
    assert len(set(cells)) == len(cells), "route overlaps itself"
    for a, b in zip(placements, placements[1:]):
        deltas = {(d[0], d[2]) for d in FACE_DELTAS.values()}
        assert (b.x - a.x, b.z - a.z) in deltas, (
            f"non-adjacent consecutive placements: {a} -> {b}"
        )


def _assert_gates_face_travel(placements: list[Placement], gate_ids: set[str]) -> None:
    """Every gate's local-north face must point at the NEXT placement."""
    from src.generation.clip_walker import GATE_FORWARD_LOCAL_FACE
    from src.catalogue.loader import rotate_face

    for a, b in zip(placements, placements[1:]):
        if a.block_id not in gate_ids:
            continue
        forward = rotate_face(GATE_FORWARD_LOCAL_FACE, a.rotation)
        delta = FACE_DELTAS[forward]
        assert (a.x + delta[0], a.z + delta[2]) == (b.x, b.z), (
            f"gate {a} arrow does not face next block {b}"
        )


class TestClipWalker:
    def test_route_closes_start_to_finish(self, synthetic_catalogue):
        walker = ClipWalker(
            synthetic_catalogue,
            ["Start", "Finish", "Checkpoint", "Straight", "Curve"],
            seed=42,
        )
        placements = walker.generate(length=20, checkpoint_every=8)
        assert placements[0].block_id == "Start"
        assert placements[-1].block_id == "Finish"
        assert any(p.block_id == "Checkpoint" for p in placements)
        assert len(placements) >= 20
        _assert_route_closed(placements)
        _assert_gates_face_travel(placements, {"Checkpoint"})

    def test_deterministic_for_seed(self, synthetic_catalogue):
        ids = ["Start", "Finish", "Straight", "Curve"]
        a = ClipWalker(synthetic_catalogue, ids, seed=7).generate(15)
        b = ClipWalker(synthetic_catalogue, ids, seed=7).generate(15)
        assert a == b

    def test_different_seeds_diverge(self, synthetic_catalogue):
        ids = ["Start", "Finish", "Straight", "Curve"]
        a = ClipWalker(synthetic_catalogue, ids, seed=1).generate(15)
        b = ClipWalker(synthetic_catalogue, ids, seed=2).generate(15)
        assert a != b

    def test_missing_start_rejected(self, synthetic_catalogue):
        with pytest.raises(ValueError):
            ClipWalker(synthetic_catalogue, ["Straight", "Curve"], seed=1)

    def test_impossible_length_raises(self, synthetic_catalogue):
        walker = ClipWalker(
            synthetic_catalogue, ["Start", "Finish", "Straight"], seed=3
        )
        # Straight-only cannot exceed the grid span; the walker must
        # fail loudly, not return a broken route.
        with pytest.raises(RouteDeadEnd):
            walker.generate(length=100, checkpoint_every=0)


def _footprint_cells(catalogue, placements: list[Placement]):
    """Recompute every occupied cell from catalogue definitions."""
    from src.catalogue.loader import rotate_offset

    cells = []
    for p in placements:
        variant = catalogue[p.block_id].variant("ground", 0)
        for unit in variant.units:
            off = rotate_offset(unit.offset, p.rotation, variant.size)
            cells.append((p.x + off[0], p.y + off[1], p.z + off[2]))
    return cells


class TestMultiCellAndElevation:
    def test_multicell_route_has_no_overlaps(self, synthetic_catalogue):
        walker = ClipWalker(
            synthetic_catalogue,
            ["Start", "Finish", "Straight", "Curve", "Curve2"],
            seed=11,
        )
        placements = walker.generate(length=25, checkpoint_every=0)
        assert any(p.block_id == "Curve2" for p in placements), (
            "seed 11 route never used the multi-cell curve; pick a seed that does"
        )
        cells = _footprint_cells(synthetic_catalogue, placements)
        assert len(cells) == len(set(cells)), "multi-cell footprints overlap"

    def test_slope_chain_gains_elevation(self, synthetic_catalogue):
        # Only slopes available between start and finish: the route
        # is forced uphill and the finish must sit above ground.
        walker = ClipWalker(
            synthetic_catalogue, ["Start", "Finish", "SlopeUp"], seed=5
        )
        placements = walker.generate(length=3, checkpoint_every=0)
        slopes = [p for p in placements if p.block_id == "SlopeUp"]
        assert slopes, "route contains no slope blocks"
        finish = placements[-1]
        start = placements[0]
        assert finish.y == start.y + len(slopes), (
            f"finish at y={finish.y}, expected start {start.y} + {len(slopes)}"
        )

    def test_wall_clips_are_not_route_ports(self, synthetic_catalogue):
        # SlopeUp carries a TrackWallVFC clip; if the walker treated
        # it as a port the block would have 3 ports and be dropped
        # from the linear pool, so no slope could ever be placed.
        walker = ClipWalker(
            synthetic_catalogue, ["Start", "Finish", "SlopeUp"], seed=5
        )
        placements = walker.generate(length=2, checkpoint_every=0)
        assert any(p.block_id == "SlopeUp" for p in placements)


@pytest.mark.skipif(not REAL_CATALOGUE.is_file(), reason="real catalogue absent")
class TestRealCatalogueSmoke:
    def test_roadtech_route(self):
        catalogue = load_catalogue(REAL_CATALOGUE)
        walker = ClipWalker(
            catalogue,
            [
                "RoadTechStart",
                "RoadTechFinish",
                "RoadTechCheckpoint",
                "RoadTechStraight",
                "RoadTechCurve1",
            ],
            seed=42,
        )
        placements = walker.generate(length=30, checkpoint_every=10)
        assert placements[0].block_id == "RoadTechStart"
        assert placements[-1].block_id == "RoadTechFinish"
        _assert_route_closed(placements)
        _assert_gates_face_travel(placements, {"RoadTechCheckpoint"})


class TestDirectionalBlocks:
    """Blocks whose EFFECT has a direction must face travel.

    Their road is 180-degree symmetric so clips accept rotation d and
    d+2 equally; only the arrow distinguishes them. Boosters shipped
    backwards roughly half the time because the gate rule had not been
    generalised to them (user-reported, 2026-07-26).
    """

    @pytest.fixture()
    def catalogue(self, tmp_path):
        records = [
            _block("Start", "Start", {"n": [CLIP]}),
            _block("Finish", "Finish", {"s": [CLIP]}),
            _block("Straight", "None", {"n": [CLIP], "s": [CLIP]}),
            # A curve, so the route can turn instead of running
            # straight off the grid before reaching the length.
            _block("Curve", "None", {"n": [CLIP], "e": [CLIP]}),
            # Symmetric road, directional effect, no waypoint kind.
            _block("RoadTechSpecialBoost", "None", {"n": [CLIP], "s": [CLIP]}),
            _block("RoadTechSpecialTurbo", "None", {"n": [CLIP], "s": [CLIP]}),
        ]
        path = tmp_path / "catalogue.ndjson"
        lines = [json.dumps({"type": "meta", "schema": "block_catalogue_v1"})]
        lines += [json.dumps(r) for r in records]
        path.write_text("\n".join(lines), encoding="utf-8")
        (tmp_path / "catalogue.done.json").write_text("{}", encoding="utf-8")
        return load_catalogue(path)

    def test_boosters_always_face_travel(self, catalogue):
        from src.generation.clip_walker import (
            DIRECTIONAL_BLOCK_PATTERNS,
            GATE_FORWARD_LOCAL_FACE,
            ClipWalker,
        )
        from src.catalogue.loader import rotate_face

        ids = ["Start", "Finish", "Straight", "Curve",
               "RoadTechSpecialBoost", "RoadTechSpecialTurbo"]
        # Several seeds: an unconstrained walker got ~half right by
        # luck, so one seed proves nothing.
        for seed in range(6):
            route = ClipWalker(catalogue, ids, seed=seed).generate(20, 0)
            placed = [
                p for p in route
                if any(pat in p.block_id for pat in DIRECTIONAL_BLOCK_PATTERNS)
            ]
            assert placed, f"seed {seed} used no directional blocks"
            for a, b in zip(route, route[1:]):
                if not any(pat in a.block_id
                           for pat in DIRECTIONAL_BLOCK_PATTERNS):
                    continue
                forward = rotate_face(GATE_FORWARD_LOCAL_FACE, a.rotation)
                dx, _dy, dz = FACE_DELTAS[forward]
                assert (a.x + dx, a.z + dz) == (b.x, b.z), (
                    f"seed {seed}: {a} points away from next block {b}"
                )
