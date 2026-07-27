"""Tests for the visits -> construction-token transform.

The properties that make the export trainable: deltas are relative and
translation-invariant, revisits are bounded back-references rather than
new vocabulary, and traversal states survive as context tokens but can
never look like placements.
"""
from __future__ import annotations

from src.learning.construction_export import ExportStats, visits_to_tokens


def _visit(i, state, pid=None, enter=0, exit_=1000):
    return {"visit_index": i, "state": state, "placement_id": pid,
            "enter_ms": enter, "exit_ms": exit_}


def _placement(block, x, y, z, rot=0):
    return {"block_type": block, "x": x, "y": y, "z": z, "rotation": rot,
            "is_free": False, "abs_x": None, "abs_y": None, "abs_z": None}


def test_places_use_relative_deltas_from_previous_block():
    placements = {
        1: _placement("Start", 10, 9, 20),
        2: _placement("Road", 10, 9, 21),
        3: _placement("Finish", 12, 10, 21),
    }
    visits = [_visit(0, "block", 1), _visit(1, "block", 2), _visit(2, "block", 3)]
    tokens = visits_to_tokens(visits, placements)

    assert [t["op"] for t in tokens] == ["PLACE", "PLACE", "PLACE"]
    assert tokens[0]["d"] == [0, 0, 0]          # origin
    assert tokens[1]["d"] == [0, 0, 1]
    assert tokens[2]["d"] == [2, 1, 0]


def test_translation_invariance():
    a = {1: _placement("A", 0, 9, 0), 2: _placement("B", 1, 9, 0)}
    b = {1: _placement("A", 30, 20, 40), 2: _placement("B", 31, 20, 40)}
    visits = [_visit(0, "block", 1), _visit(1, "block", 2)]
    ta = visits_to_tokens(visits, a)
    tb = visits_to_tokens(visits, b)
    assert [t["d"] for t in ta] == [t["d"] for t in tb]


def test_revisit_is_a_bounded_back_reference():
    placements = {
        1: _placement("A", 0, 9, 0),
        2: _placement("B", 0, 9, 1),
        3: _placement("C", 0, 9, 2),
    }
    # Drive A, B, C, then back over B: a loop crossing.
    visits = [_visit(0, "block", 1), _visit(1, "block", 2),
              _visit(2, "block", 3), _visit(3, "block", 2)]
    tokens = visits_to_tokens(visits, placements)

    assert [t["op"] for t in tokens] == ["PLACE", "PLACE", "PLACE", "REVISIT"]
    # B is 2 back from the most recent placement (C=1, B=2).
    assert tokens[3]["back"] == 2
    # No second PLACE for B: revisits never grow the construction.
    assert sum(1 for t in tokens if t["op"] == "PLACE") == 3


def test_delta_after_revisit_is_from_the_revisited_block():
    placements = {
        1: _placement("A", 0, 9, 0),
        2: _placement("B", 0, 9, 5),
        3: _placement("C", 0, 9, 1),
    }
    # A -> B -> back to A -> place C next to A.
    visits = [_visit(0, "block", 1), _visit(1, "block", 2),
              _visit(2, "block", 1), _visit(3, "block", 3)]
    tokens = visits_to_tokens(visits, placements)
    # C's delta must be relative to A (where the car is), not B (last
    # placed): the cursor follows the car.
    assert tokens[-1]["op"] == "PLACE"
    assert tokens[-1]["d"] == [0, 0, 1]


def test_traversal_states_are_context_not_placements():
    placements = {1: _placement("A", 0, 9, 0), 2: _placement("B", 0, 9, 4)}
    visits = [_visit(0, "block", 1), _visit(1, "airborne"),
              _visit(2, "off_surface"), _visit(3, "block", 2)]
    tokens = visits_to_tokens(visits, placements)
    ops = [t["op"] for t in tokens]
    assert ops == ["PLACE", "JUMP", "GAP", "PLACE"]
    assert all("block" not in t for t in tokens if t["op"] in ("JUMP", "GAP"))


def test_adjacent_traversal_runs_collapse():
    placements = {1: _placement("A", 0, 9, 0)}
    visits = [_visit(0, "block", 1), _visit(1, "airborne"),
              _visit(2, "airborne"), _visit(3, "airborne")]
    tokens = visits_to_tokens(visits, placements)
    assert [t["op"] for t in tokens] == ["PLACE", "JUMP"]


def test_stats_count_fast_revisits():
    placements = {1: _placement("A", 0, 9, 0), 2: _placement("B", 0, 9, 1)}
    visits = [_visit(0, "block", 1), _visit(1, "block", 2),
              _visit(2, "block", 1, enter=1000, exit_=1100)]
    stats = ExportStats()
    visits_to_tokens(visits, placements, stats)
    assert stats.revisits == 1
    assert stats.fast_revisits == 1
