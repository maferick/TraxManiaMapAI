"""Unit tests for the support-pillar pass (v4, game-harvested rules).

The authoritative check on this module is
``tools/verify_supports.py``, which diffs its output against what the
game itself generates. These tests pin the mechanics that diff would
only catch as a mystery: schema handling, refusal to guess, and the
rotation transform that broke a dirt route.
"""
from __future__ import annotations

import json

import pytest

from src.catalogue.loader import load_catalogue
from src.generation.clip_walker import GROUND_Y, Placement
from src.generation.supports import (
    PillarRule,
    PillarRules,
    PillarSlot,
    build_supports,
    route_cells,
)

ONE = [[0, 0, 0]]
TWO_BY_TWO = [[0, 0, 0], [1, 0, 0], [0, 0, 1], [1, 0, 1]]
DEEP_1X2 = [[0, 0, 0], [0, 0, 1]]


def _block(block_id, size, offsets, is_pillar=False):
    return {
        "type": "block", "id": block_id, "name": block_id, "page": "p",
        "waypoint": "None", "is_pillar": is_pillar,
        "variants": [{
            "kind": "ground", "index": 0, "size": list(size),
            "units": [
                {"offset": list(o), "underground": False,
                 "terrain_modifier": "", "surface": "", "clips": {}}
                for o in offsets
            ],
        }],
    }


@pytest.fixture()
def catalogue(tmp_path):
    records = [
        _block("Straight", (1, 1, 1), ONE),
        _block("Curve2", (2, 1, 2), TWO_BY_TWO),
        _block("Transition", (1, 1, 2), DEEP_1X2),
        _block("StraightPillar", (1, 1, 1), ONE, is_pillar=True),
        _block("Curve2Pillar", (2, 1, 2), TWO_BY_TWO, is_pillar=True),
    ]
    path = tmp_path / "catalogue.ndjson"
    lines = [json.dumps({"type": "meta", "schema": "block_catalogue_v1"})]
    lines += [json.dumps(r) for r in records]
    path.write_text("\n".join(lines), encoding="utf-8")
    (tmp_path / "catalogue.done.json").write_text("{}", encoding="utf-8")
    return load_catalogue(path)


def _rules(mapping: dict[str, PillarRule]) -> PillarRules:
    return PillarRules(mapping, GROUND_Y)


@pytest.fixture()
def rules():
    return _rules({
        "Straight": PillarRule(
            pattern=(PillarSlot(0, 0, "StraightPillar", 0, 2),), uniform=True),
        "Curve2": PillarRule(
            pattern=(PillarSlot(0, 0, "Curve2Pillar", 1, 0),), uniform=True),
        # Two slots that differ, as a real RoadTech->RoadBump does.
        "Transition": PillarRule(
            pattern=(
                PillarSlot(0, 0, "StraightPillar", 0, 2),
                PillarSlot(0, 1, "StraightPillar", 0, 0),
            ), uniform=True),
    })


class TestSchema:
    def test_rejects_old_schema(self, tmp_path):
        p = tmp_path / "rules.json"
        p.write_text(json.dumps({"schema": "pillar_rules_v2", "rules": {}}))
        with pytest.raises(ValueError, match="unsupported pillar-rule schema"):
            PillarRules.load(p)

    def test_loads_pattern(self, tmp_path):
        p = tmp_path / "rules.json"
        p.write_text(json.dumps({
            "schema": "pillar_rules_v3", "ground_y": 9,
            "rules": {"A": {"pattern": [
                {"dx": 0, "dz": 1, "pillar": "P", "variant": 3, "dir": 2}
            ], "uniform": True}},
        }))
        loaded = PillarRules.load(p)
        rule = loaded.get("A")
        assert rule is not None and len(rule.pattern) == 1
        slot = rule.pattern[0]
        assert (slot.dz, slot.variant, slot.direction) == (1, 3, 2)


class TestBuildSupports:
    def test_ground_level_needs_no_pillars(self, catalogue, rules):
        route = [Placement("Straight", 10, GROUND_Y, 10, 0)]
        assert build_supports(route, catalogue, rules) == []

    def test_column_filled_with_slot_values(self, catalogue, rules):
        route = [Placement("Straight", 10, GROUND_Y + 3, 10, 0)]
        pillars = build_supports(route, catalogue, rules)
        assert [p.y for p in pillars] == [GROUND_Y, GROUND_Y + 1, GROUND_Y + 2]
        assert all(p.block_id == "StraightPillar" for p in pillars)
        assert all(p.variant == 0 for p in pillars)
        assert all(p.rotation == 2 for p in pillars)

    def test_multi_slot_pattern_emits_every_slot(self, catalogue, rules):
        route = [Placement("Transition", 10, GROUND_Y + 2, 10, 0)]
        pillars = build_supports(route, catalogue, rules)
        # 2 slots x 2 levels
        assert len(pillars) == 4
        assert {(p.x, p.z) for p in pillars} == {(10, 10), (10, 11)}
        # the two slots keep their distinct directions
        assert {p.rotation for p in pillars} == {0, 2}

    def test_multicell_pillar_anchor_survives_rotation(self, catalogue, rules):
        """A 2x2 pillar must stay on the block's anchor at every rotation.

        Rotating the slot offset through rotate_offset re-anchors and
        shifted this to (x+1, z) at rotation 1 — 15 wrong cells on a
        real dirt route before the fix.
        """
        for rot in range(4):
            route = [Placement("Curve2", 20, GROUND_Y + 1, 20, rot)]
            pillars = build_supports(route, catalogue, rules)
            assert len(pillars) == 1, rot
            assert (pillars[0].x, pillars[0].z) == (20, 20), rot

    def test_rotation_offsets_multi_slot(self, catalogue, rules):
        # At rotation 1 the 1x2 footprint runs along x, so the two
        # slots must land side by side in x, not stacked in z.
        route = [Placement("Transition", 10, GROUND_Y + 1, 10, 1)]
        pillars = build_supports(route, catalogue, rules)
        assert {(p.x, p.z) for p in pillars} == {(10, 10), (11, 10)}

    def test_unknown_block_gets_no_pillars(self, catalogue, rules):
        route = [Placement("Curve2", 10, GROUND_Y + 2, 10, 0)]
        empty = _rules({})
        assert build_supports(route, catalogue, empty) == []

    def test_non_uniform_is_skipped_not_guessed(self, catalogue):
        skewed = _rules({"Straight": PillarRule(
            pattern=(PillarSlot(0, 0, "StraightPillar", 0, 0),),
            uniform=False)})
        route = [Placement("Straight", 10, GROUND_Y + 3, 10, 0)]
        assert build_supports(route, catalogue, skewed) == []

    def test_pillars_never_intersect_the_route(self, catalogue, rules):
        route = [
            Placement("Straight", 10, GROUND_Y, 10, 0),
            Placement("Straight", 10, GROUND_Y + 2, 10, 0),
        ]
        cells = route_cells(route, catalogue)
        pillars = build_supports(route, catalogue, rules)
        assert all((p.x, p.y, p.z) not in cells for p in pillars)
        assert [p.y for p in pillars] == [GROUND_Y + 1]

    def test_lowest_block_owns_a_stacked_column(self, catalogue, rules):
        """Bottom-up ordering: the lower block claims the column.

        Route order let an upper 1x1 grab a column the lower 2x2
        owned, which the game-diff caught as 24 wrong cells.
        """
        route = [
            Placement("Straight", 20, GROUND_Y + 4, 20, 0),  # upper, listed first
            Placement("Curve2", 20, GROUND_Y + 2, 20, 0),    # lower
        ]
        pillars = build_supports(route, catalogue, rules)
        by_y = {p.y: p.block_id for p in pillars}
        # Below the lower block: owned by it, despite being listed second.
        assert by_y[GROUND_Y] == "Curve2Pillar"
        assert by_y[GROUND_Y + 1] == "Curve2Pillar"
        # Its own level is a route cell, so no pillar there.
        assert GROUND_Y + 2 not in by_y
        # The gap between the two decks is supported by the upper block.
        assert by_y[GROUND_Y + 3] == "StraightPillar"
