"""Unit tests for the support-pillar pass (v2, footprint-matched)."""
from __future__ import annotations

import json

import pytest

from src.catalogue.loader import load_catalogue
from src.generation.clip_walker import GROUND_Y, Placement
from src.generation.supports import (
    DEFAULT_PILLAR,
    build_supports,
    pillar_for,
    route_cells,
)

CLIP = "RoadTechFC"


def _block(block_id: str, size, offsets, is_pillar=False) -> dict:
    return {
        "type": "block", "id": block_id, "name": block_id,
        "page": "p", "waypoint": "None", "is_pillar": is_pillar,
        "variants": [{
            "kind": "ground", "index": 0, "size": list(size),
            "units": [
                {"offset": list(o), "underground": False,
                 "terrain_modifier": "", "surface": "", "clips": {}}
                for o in offsets
            ],
        }],
    }


ONE = [[0, 0, 0]]
TWO_BY_TWO = [[0, 0, 0], [1, 0, 0], [0, 0, 1], [1, 0, 1]]


@pytest.fixture()
def catalogue(tmp_path):
    records = [
        _block("RoadTechStraight", (1, 1, 1), ONE),
        _block("RoadTechCurve2", (2, 1, 2), TWO_BY_TWO),
        _block("RoadTechSlopeBase", (1, 2, 1), [[0, 0, 0], [0, 1, 0]]),
        # matching pillars
        _block(DEFAULT_PILLAR, (1, 1, 1), ONE, is_pillar=True),
        _block("TrackWallCurve2Pillar", (2, 1, 2), TWO_BY_TWO, is_pillar=True),
        # a name-matching pillar with the WRONG footprint: must be rejected
        _block("TrackWallStraightPillar", (1, 1, 1), ONE, is_pillar=True),
    ]
    path = tmp_path / "catalogue.ndjson"
    lines = [json.dumps({"type": "meta", "schema": "block_catalogue_v1"})]
    lines += [json.dumps(r) for r in records]
    path.write_text("\n".join(lines), encoding="utf-8")
    (tmp_path / "catalogue.done.json").write_text("{}", encoding="utf-8")
    return load_catalogue(path)


class TestPillarFor:
    def test_shape_matched_by_name_rewrite(self, catalogue):
        assert pillar_for("RoadTechCurve2", catalogue) == "TrackWallCurve2Pillar"

    def test_single_cell_falls_back(self, catalogue):
        # No TrackWallSlopeBasePillar exists, but the footprint is 1x1.
        assert pillar_for("RoadTechSlopeBase", catalogue) == DEFAULT_PILLAR

    def test_unknown_block_returns_none(self, catalogue):
        assert pillar_for("NotAThing", catalogue) is None


class TestBuildSupports:
    def test_ground_level_needs_no_pillars(self, catalogue):
        route = [Placement("RoadTechStraight", 10, GROUND_Y, 10, 0)]
        assert build_supports(route, catalogue) == []

    def test_one_pillar_per_level_not_per_cell(self, catalogue):
        # A 2x2 curve three levels up: v1 produced 4 cells x 3 levels
        # = 12 blocks (the concrete-plateau bug). v2 must emit 3.
        route = [Placement("RoadTechCurve2", 4, GROUND_Y + 3, 6, 0)]
        pillars = build_supports(route, catalogue)
        assert len(pillars) == 3
        assert all(p.block_id == "TrackWallCurve2Pillar" for p in pillars)
        assert [p.y for p in pillars] == [GROUND_Y, GROUND_Y + 1, GROUND_Y + 2]

    def test_wide_pillar_inherits_anchor_and_rotation(self, catalogue):
        route = [Placement("RoadTechCurve2", 4, GROUND_Y + 1, 6, 3)]
        pillars = build_supports(route, catalogue)
        assert [(p.x, p.z, p.rotation) for p in pillars] == [(4, 6, 3)]

    def test_single_cell_stack_matches_game_recipe(self, catalogue):
        """Reproduces the observed auto-pillar stack exactly.

        Ground truth from a hand-placed RoadTechStraight at y=18:
        nine TrackWallStraightPillar, all facing North, variants
        1 (shaft) down to 5 (transition) then 0 (foot).
        """
        from src.generation.supports import (
            PILLAR_VARIANT_FOOT,
            PILLAR_VARIANT_SHAFT,
            PILLAR_VARIANT_TRANSITION,
        )
        route = [Placement("RoadTechStraight", 15, GROUND_Y + 9, 24, 2)]
        pillars = build_supports(route, catalogue)
        assert len(pillars) == 9
        assert all(p.block_id == DEFAULT_PILLAR for p in pillars)
        # North regardless of the road's rotation (2 = South)
        assert all(p.rotation == 0 for p in pillars)
        by_y = {p.y: p.variant for p in pillars}
        assert by_y[GROUND_Y] == PILLAR_VARIANT_FOOT
        assert by_y[GROUND_Y + 1] == PILLAR_VARIANT_TRANSITION
        assert all(
            by_y[y] == PILLAR_VARIANT_SHAFT
            for y in range(GROUND_Y + 2, GROUND_Y + 9)
        )

    def test_pillars_never_intersect_the_route(self, catalogue):
        route = [
            Placement("RoadTechStraight", 10, GROUND_Y, 10, 0),
            Placement("RoadTechStraight", 10, GROUND_Y + 2, 10, 0),
        ]
        cells = route_cells(route, catalogue)
        pillars = build_supports(route, catalogue)
        for p in pillars:
            assert (p.x, p.y, p.z) not in cells
        assert [p.y for p in pillars] == [GROUND_Y + 1]

    def test_no_two_pillars_share_a_cell(self, catalogue):
        route = [
            Placement("RoadTechCurve2", 4, GROUND_Y + 3, 6, 0),
            Placement("RoadTechStraight", 5, GROUND_Y + 2, 7, 0),
        ]
        pillars = build_supports(route, catalogue)
        seen = set()
        for p in pillars:
            fp = route_cells([p], catalogue)
            assert not (fp & seen), f"pillar {p} overlaps an earlier pillar"
            seen |= fp
