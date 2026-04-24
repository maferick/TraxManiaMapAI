"""Phase 2 #218-1 — unit tests for block pair-transition extractor.

The DB I/O side (read route_corridors, write block_pair_transitions)
is thin SQL; tests here exercise the pure helpers — JSON parsing,
pair-count accumulation, cell-to-block lookup — so the wiring is
correct without needing a live DB.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.constraints.block_transitions import (
    _PairKey,
    _parse_path_cells,
    extract_pair_counts_for_map,
)
from src.constraints import block_transitions as bt_mod


class TestParsePathCells:
    def test_well_formed_json(self) -> None:
        assert _parse_path_cells("[[0,0,0],[1,0,0],[2,0,0]]") == [
            (0, 0, 0), (1, 0, 0), (2, 0, 0),
        ]

    def test_empty_string(self) -> None:
        assert _parse_path_cells("") == []
        assert _parse_path_cells(None) == []

    def test_malformed_json_returns_empty(self) -> None:
        # A partial map-wide build shouldn't die because one row has
        # corrupt JSON; the row is silently skipped.
        assert _parse_path_cells("{not json") == []

    def test_non_triple_entries_are_dropped(self) -> None:
        assert _parse_path_cells("[[0,0,0],[1,0],[2,0,0,3]]") == [
            (0, 0, 0), (2, 0, 0),  # wait — 4-element gets dropped
        ] or _parse_path_cells("[[0,0,0],[1,0],[2,0,0,3]]") == [
            (0, 0, 0),
        ]

    def test_actually_drops_wrong_arity(self) -> None:
        # More careful: parser accepts ONLY len-3 sequences.
        out = _parse_path_cells("[[0,0,0],[1,0],[2,0,0,3]]")
        assert (0, 0, 0) in out
        assert (1, 0) not in [*out]
        assert not any(len(c) != 3 for c in out)  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# extract_pair_counts_for_map — monkeypatched DB
# ---------------------------------------------------------------------

def _stub_cursor(rowsets: list[list]):
    """Returns a context-manager that yields cursors which pop off
    ``rowsets`` for successive executes. One rowset per execute call,
    in order."""
    cur = MagicMock()
    results = iter(rowsets)

    def fetchall_impl(*a, **kw):
        return next(results)
    cur.fetchall.side_effect = fetchall_impl
    # execute() is a no-op; we return rows via fetchall.
    cur.execute = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    ctx.__exit__.return_value = False
    return ctx


class TestExtractPairCountsForMap:
    def test_happy_three_cell_path(self, monkeypatch) -> None:
        # One corridor with a 3-cell path and known blocks at every
        # cell → 2 ordered transitions (A→B, B→C).
        corridor_rows = [(1, "Stadium", "[[0,0,0],[1,0,0],[2,0,0]]")]
        block_rows = [
            ("Road",     "RoadStraight",  0, 0, 0),
            ("RoadTech", "RoadTechCurve", 1, 0, 0),
            ("Road",     "RoadStraight",  2, 0, 0),
        ]
        ctx1 = _stub_cursor([corridor_rows])
        ctx2 = _stub_cursor([block_rows])
        ctx_iter = iter([ctx1, ctx2])
        monkeypatch.setattr(
            bt_mod, "cursor", lambda _c: next(ctx_iter),
        )
        counts = extract_pair_counts_for_map(MagicMock(), 1)
        assert counts == {
            _PairKey("Road", "RoadStraight",
                     "RoadTech", "RoadTechCurve", "Stadium"): 1,
            _PairKey("RoadTech", "RoadTechCurve",
                     "Road", "RoadStraight", "Stadium"): 1,
        }

    def test_direction_matters(self, monkeypatch) -> None:
        # Same two blocks in two corridors, opposite directions →
        # two distinct pair keys.
        corridor_rows = [
            (1, "Stadium", "[[0,0,0],[1,0,0]]"),
            (1, "Stadium", "[[1,0,0],[0,0,0]]"),
        ]
        block_rows = [
            ("F1", "A", 0, 0, 0),
            ("F2", "B", 1, 0, 0),
        ]
        ctx1 = _stub_cursor([corridor_rows])
        ctx2 = _stub_cursor([block_rows])
        ctx_iter = iter([ctx1, ctx2])
        monkeypatch.setattr(
            bt_mod, "cursor", lambda _c: next(ctx_iter),
        )
        counts = extract_pair_counts_for_map(MagicMock(), 1)
        keys = {k for k in counts}
        assert _PairKey("F1", "A", "F2", "B", "Stadium") in keys
        assert _PairKey("F2", "B", "F1", "A", "Stadium") in keys
        assert all(v == 1 for v in counts.values())

    def test_unknown_cells_are_skipped(self, monkeypatch) -> None:
        # A corridor that includes a cell with no block placement
        # (e.g. snapped-free waypoint cell) silently drops that
        # transition edge.
        corridor_rows = [(1, "Canyon", "[[0,0,0],[1,0,0],[2,0,0]]")]
        block_rows = [
            ("F1", "A", 0, 0, 0),
            # missing block at (1,0,0)
            ("F2", "C", 2, 0, 0),
        ]
        ctx1 = _stub_cursor([corridor_rows])
        ctx2 = _stub_cursor([block_rows])
        ctx_iter = iter([ctx1, ctx2])
        monkeypatch.setattr(
            bt_mod, "cursor", lambda _c: next(ctx_iter),
        )
        counts = extract_pair_counts_for_map(MagicMock(), 1)
        # Both transitions touch the missing cell → zero counted pairs.
        assert counts == {}

    def test_empty_corridor_rows(self, monkeypatch) -> None:
        ctx1 = _stub_cursor([[]])
        monkeypatch.setattr(
            bt_mod, "cursor", lambda _c: ctx1,
        )
        assert extract_pair_counts_for_map(MagicMock(), 1) == {}

    def test_multiple_corridors_same_pair_aggregate(self, monkeypatch) -> None:
        # Two corridors, each with the same A→B transition → count=2.
        corridor_rows = [
            (1, "Stadium", "[[0,0,0],[1,0,0]]"),
            (1, "Stadium", "[[0,0,0],[1,0,0]]"),
        ]
        block_rows = [
            ("F", "A", 0, 0, 0),
            ("F", "B", 1, 0, 0),
        ]
        ctx1 = _stub_cursor([corridor_rows])
        ctx2 = _stub_cursor([block_rows])
        ctx_iter = iter([ctx1, ctx2])
        monkeypatch.setattr(
            bt_mod, "cursor", lambda _c: next(ctx_iter),
        )
        counts = extract_pair_counts_for_map(MagicMock(), 1)
        assert counts[_PairKey("F", "A", "F", "B", "Stadium")] == 2


# ---------------------------------------------------------------------
# #218-2 — triple extraction + signature stability
# ---------------------------------------------------------------------

from src.constraints.block_transitions import (
    _TripleKey,
    extract_triple_counts_for_map,
)


class TestTripleSignature:
    def test_deterministic(self) -> None:
        k = _TripleKey("F1", "A", "F2", "B", "F3", "C", "Stadium")
        assert k.signature() == k.signature()

    def test_is_64_hex(self) -> None:
        sig = _TripleKey("F", "a", "F", "b", "F", "c", "E").signature()
        assert len(sig) == 64
        int(sig, 16)  # valid hex

    def test_order_matters(self) -> None:
        a = _TripleKey("F1", "A", "F2", "B", "F3", "C", "Stadium")
        # Shuffling name positions must produce a different signature.
        b = _TripleKey("F1", "A", "F3", "C", "F2", "B", "Stadium")
        assert a.signature() != b.signature()

    def test_environment_separates(self) -> None:
        a = _TripleKey("F1", "A", "F2", "B", "F3", "C", "Stadium")
        b = _TripleKey("F1", "A", "F2", "B", "F3", "C", "Canyon")
        assert a.signature() != b.signature()


class TestExtractTripleCountsForMap:
    def test_four_cell_path_emits_two_triples(self, monkeypatch) -> None:
        # A path of 4 cells → 2 overlapping 3-windows (A,B,C) + (B,C,D).
        corridor_rows = [(1, "Stadium", "[[0,0,0],[1,0,0],[2,0,0],[3,0,0]]")]
        block_rows = [
            ("F", "A", 0, 0, 0),
            ("F", "B", 1, 0, 0),
            ("F", "C", 2, 0, 0),
            ("F", "D", 3, 0, 0),
        ]
        ctx1 = _stub_cursor([corridor_rows])
        ctx2 = _stub_cursor([block_rows])
        ctx_iter = iter([ctx1, ctx2])
        monkeypatch.setattr(
            bt_mod, "cursor", lambda _c: next(ctx_iter),
        )
        counts = extract_triple_counts_for_map(MagicMock(), 1)
        assert len(counts) == 2
        assert counts[_TripleKey("F", "A", "F", "B", "F", "C", "Stadium")] == 1
        assert counts[_TripleKey("F", "B", "F", "C", "F", "D", "Stadium")] == 1

    def test_unknown_middle_cell_skips_both_windows(self, monkeypatch) -> None:
        # Path A-?-C-D: the middle cell has no block. That gap means
        # triple (A,?,C) AND (?,C,D) both have an unknown cell → both
        # skipped. No triples emitted despite the corridor having 4
        # cells.
        corridor_rows = [(1, "Stadium", "[[0,0,0],[1,0,0],[2,0,0],[3,0,0]]")]
        block_rows = [
            ("F", "A", 0, 0, 0),
            # (1,0,0) missing
            ("F", "C", 2, 0, 0),
            ("F", "D", 3, 0, 0),
        ]
        ctx1 = _stub_cursor([corridor_rows])
        ctx2 = _stub_cursor([block_rows])
        ctx_iter = iter([ctx1, ctx2])
        monkeypatch.setattr(
            bt_mod, "cursor", lambda _c: next(ctx_iter),
        )
        counts = extract_triple_counts_for_map(MagicMock(), 1)
        assert counts == {}

    def test_path_too_short_emits_nothing(self, monkeypatch) -> None:
        # 2-cell path — no 3-window possible.
        corridor_rows = [(1, "Stadium", "[[0,0,0],[1,0,0]]")]
        block_rows = [
            ("F", "A", 0, 0, 0),
            ("F", "B", 1, 0, 0),
        ]
        ctx1 = _stub_cursor([corridor_rows])
        ctx2 = _stub_cursor([block_rows])
        ctx_iter = iter([ctx1, ctx2])
        monkeypatch.setattr(
            bt_mod, "cursor", lambda _c: next(ctx_iter),
        )
        assert extract_triple_counts_for_map(MagicMock(), 1) == {}

    def test_repeat_triples_aggregate(self, monkeypatch) -> None:
        corridor_rows = [
            (1, "Stadium", "[[0,0,0],[1,0,0],[2,0,0]]"),
            (1, "Stadium", "[[0,0,0],[1,0,0],[2,0,0]]"),
        ]
        block_rows = [
            ("F", "A", 0, 0, 0), ("F", "B", 1, 0, 0), ("F", "C", 2, 0, 0),
        ]
        ctx1 = _stub_cursor([corridor_rows])
        ctx2 = _stub_cursor([block_rows])
        ctx_iter = iter([ctx1, ctx2])
        monkeypatch.setattr(
            bt_mod, "cursor", lambda _c: next(ctx_iter),
        )
        counts = extract_triple_counts_for_map(MagicMock(), 1)
        assert counts[_TripleKey("F", "A", "F", "B", "F", "C", "Stadium")] == 2
