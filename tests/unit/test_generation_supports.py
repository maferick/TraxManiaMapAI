"""Unit tests for the support-pillar pass."""
from __future__ import annotations

import json

import pytest

from src.catalogue.loader import load_catalogue
from src.generation.clip_walker import GROUND_Y, Placement
from src.generation.supports import (
    DEFAULT_PILLAR,
    build_supports,
    route_cells,
)

CLIP = "RoadTechFC"


def _one_by_one(block_id: str) -> dict:
    return {
        "type": "block", "id": block_id, "name": block_id,
        "page": "p", "waypoint": "None", "is_pillar": True,
        "variants": [{
            "kind": "ground", "index": 0, "size": [1, 1, 1],
            "units": [{"offset": [0, 0, 0], "underground": False,
                       "terrain_modifier": "", "surface": "",
                       "clips": {"n": [CLIP], "s": [CLIP]}}],
        }],
    }


def _two_by_two(block_id: str) -> dict:
    return {
        "type": "block", "id": block_id, "name": block_id,
        "page": "p", "waypoint": "None", "is_pillar": False,
        "variants": [{
            "kind": "ground", "index": 0, "size": [2, 1, 2],
            "units": [
                {"offset": o, "underground": False, "terrain_modifier": "",
                 "surface": "", "clips": {}}
                for o in ([0, 0, 0], [1, 0, 0], [0, 0, 1], [1, 0, 1])
            ],
        }],
    }


@pytest.fixture()
def catalogue(tmp_path):
    records = [
        _one_by_one("Straight"),
        _one_by_one(DEFAULT_PILLAR),
        _two_by_two("Curve2"),
    ]
    path = tmp_path / "catalogue.ndjson"
    lines = [json.dumps({"type": "meta", "schema": "block_catalogue_v1"})]
    lines += [json.dumps(r) for r in records]
    path.write_text("\n".join(lines), encoding="utf-8")
    (tmp_path / "catalogue.done.json").write_text("{}", encoding="utf-8")
    return load_catalogue(path)


class TestBuildSupports:
    def test_ground_level_needs_no_pillars(self, catalogue):
        route = [Placement("Straight", 10, GROUND_Y, 10, 0)]
        assert build_supports(route, catalogue) == []

    def test_elevated_block_filled_to_ground(self, catalogue):
        route = [Placement("Straight", 10, GROUND_Y + 3, 10, 0)]
        pillars = build_supports(route, catalogue)
        assert [p.y for p in pillars] == [GROUND_Y, GROUND_Y + 1, GROUND_Y + 2]
        assert all(p.block_id == DEFAULT_PILLAR for p in pillars)
        assert all((p.x, p.z) == (10, 10) for p in pillars)

    def test_multicell_gets_a_column_per_cell(self, catalogue):
        route = [Placement("Curve2", 4, GROUND_Y + 2, 6, 0)]
        pillars = build_supports(route, catalogue)
        # 4 footprint cells x 2 levels below
        assert len(pillars) == 8
        assert {(p.x, p.z) for p in pillars} == {(4, 6), (5, 6), (4, 7), (5, 7)}

    def test_pillars_never_collide_with_route(self, catalogue):
        # A route that climbs then returns to ground in the SAME column
        # must not get a pillar inside its own lower block.
        route = [
            Placement("Straight", 10, GROUND_Y, 10, 0),
            Placement("Straight", 10, GROUND_Y + 2, 10, 0),
        ]
        pillars = build_supports(route, catalogue)
        cells = route_cells(route, catalogue)
        assert all((p.x, p.y, p.z) not in cells for p in pillars)
        # only the gap at GROUND_Y+1 is filled
        assert [(p.x, p.y, p.z) for p in pillars] == [(10, GROUND_Y + 1, 10)]

    def test_no_duplicate_pillar_cells(self, catalogue):
        route = [
            Placement("Curve2", 4, GROUND_Y + 3, 6, 0),
            Placement("Straight", 5, GROUND_Y + 1, 7, 0),
        ]
        pillars = build_supports(route, catalogue)
        keys = [(p.x, p.y, p.z) for p in pillars]
        assert len(keys) == len(set(keys))

    def test_unknown_pillar_rejected(self, catalogue):
        with pytest.raises(KeyError):
            build_supports([], catalogue, pillar_id="NoSuchPillar")
