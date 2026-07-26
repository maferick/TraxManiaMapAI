"""Unit tests for the generation-time grammar view."""
from __future__ import annotations

import json

import pytest

from src.generation.grammar import Move, PlacementGrammar


@pytest.fixture()
def grammar(tmp_path):
    doc = {
        "schema": "placement_grammar_v1",
        "environment": "Stadium2020",
        "min_maps": 3,
        "offsets": [[0, 0, 1], [0, 0, 0], [0, 0, 3], [1, 0, 0]],
        "rules": {
            "Straight": [
                ["Straight", 0, 0, 900, 40000, 1],
                ["Curve", 3, 1, 400, 900, 1],
                ["GateCheckpoint", 1, 0, 120, 300, 0],
                ["Straight", 2, 0, 30, 60, 0],
            ],
        },
    }
    path = tmp_path / "grammar.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return PlacementGrammar.from_json(path)


class TestLoad:
    def test_rejects_a_foreign_schema(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"schema": "nope"}), encoding="utf-8")
        with pytest.raises(ValueError, match="schema"):
            PlacementGrammar.from_json(path)

    def test_moves_resolve_their_offsets(self, grammar):
        moves = grammar.successors("Straight")
        assert moves[0].block == "Straight"
        assert moves[0].offset == (0, 0, 1)


class TestSuccessorFilters:
    def test_min_maps_drops_the_tail(self, grammar):
        assert len(grammar.successors("Straight", min_maps=100)) == 3

    def test_allow_restricts_the_vocabulary(self, grammar):
        moves = grammar.successors("Straight", allow=frozenset({"Curve"}))
        assert [m.block for m in moves] == ["Curve"]

    def test_overlays_are_selectable(self, grammar):
        """A clipless gate shares the route's cell rather than following it."""
        moves = grammar.successors("Straight", overlays=True)
        assert [m.block for m in moves] == ["GateCheckpoint"]
        assert all(not m.is_overlay for m in
                   grammar.successors("Straight", overlays=False))

    def test_gaps_are_selectable(self, grammar):
        moves = grammar.successors("Straight", gaps=True)
        assert [m.offset for m in moves] == [(0, 0, 3)]

    def test_unknown_block_has_no_successors(self, grammar):
        assert grammar.successors("NotAThing") == ()


class TestApply:
    def test_offset_follows_the_source_rotation(self):
        """A pattern learned facing north must apply at every heading."""
        move = Move("Curve", (0, 0, 1), 1, 10, 10, True)
        # Source facing north (rotation 0): +z.
        assert move.apply((5, 9, 5), 0) == ((5, 9, 6), 1)
        # Rotated one step: the same local +z is now world -x.
        assert move.apply((5, 9, 5), 1) == ((4, 9, 5), 2)
        assert move.apply((5, 9, 5), 2) == ((5, 9, 4), 3)
        assert move.apply((5, 9, 5), 3) == ((6, 9, 5), 0)

    def test_elevation_is_carried_through(self):
        move = Move("Slope", (0, 1, 1), 0, 10, 10, True)
        assert move.apply((5, 9, 5), 0) == ((5, 10, 6), 0)
