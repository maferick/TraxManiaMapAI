"""Phase-2 PR D — pure-function tests for assemble_route_from_inputs.

Exercises every reject_reason branch + the happy path. No DB, no
files; tests build :class:`AssemblyInputs` directly.
"""
from __future__ import annotations

import pytest

from src.generation import (
    Anchor,
    AssembledRoute,
    AssemblyError,
    AssemblyInputs,
    CandidateCorridor,
    assemble_route_from_inputs,
)
from src.generation.assembly import (
    _cells_continuous,
    _expected_time_ms,
    _tie_break_key,
)


def _anchor(tag: str, order: int, cell=(0, 0, 0)) -> Anchor:
    return Anchor(tag=tag, order=order, cell=cell)


def _candidate(
    *,
    corridor_id: int = 1,
    map_id: int = 42,
    src: Anchor | None = None,
    dst: Anchor | None = None,
    cells: tuple = ((0, 0, 0), (0, 0, 1)),
    length: int | None = None,
    virtual: bool = False,
    conf: float | None = 0.5,
    learned: float | None = 0.5,
) -> CandidateCorridor:
    src = src or _anchor("Spawn", 0, cells[0])
    dst = dst or _anchor("Goal", 0, cells[-1])
    return CandidateCorridor(
        corridor_id=corridor_id,
        map_id=map_id,
        src=src, dst=dst,
        path_cells=cells,
        path_length=length if length is not None else len(cells),
        contains_virtual_edge=virtual,
        corridor_confidence=conf,
        learned_corridor_score=learned,
    )


# ---------------------------------------------------------------------
# Helper primitives
# ---------------------------------------------------------------------

class TestExpectedTimeMs:
    def test_zero_length_zero_time(self) -> None:
        assert _expected_time_ms(0) == 0
        assert _expected_time_ms(-1) == 0

    def test_positive_scales_linearly(self) -> None:
        # 3 cells × 32m / 30 m/s × 1000 = 3200 ms (same physics as
        # the time_envelope label; don't redefine constants here).
        assert _expected_time_ms(3) == 3200

    def test_doubling_length_doubles_time(self) -> None:
        assert _expected_time_ms(6) == 2 * _expected_time_ms(3)


class TestCellsContinuous:
    def test_same_cell_is_continuous(self) -> None:
        assert _cells_continuous((5, 5, 5), (5, 5, 5))

    def test_adjacent_cells_are_continuous(self) -> None:
        assert _cells_continuous((5, 5, 5), (5, 5, 6))
        assert _cells_continuous((5, 5, 5), (6, 5, 5))
        # Chebyshev distance 1 includes diagonals.
        assert _cells_continuous((5, 5, 5), (6, 6, 6))

    def test_distant_cells_are_not_continuous(self) -> None:
        assert not _cells_continuous((5, 5, 5), (5, 5, 7))
        assert not _cells_continuous((5, 5, 5), (10, 10, 10))


class TestTieBreakKey:
    def test_higher_score_wins(self) -> None:
        a = _candidate(corridor_id=1, learned=0.7, length=5)
        b = _candidate(corridor_id=2, learned=0.9, length=5)
        ordered = sorted([a, b], key=_tie_break_key)
        assert ordered[0].corridor_id == 2

    def test_same_score_shorter_path_wins(self) -> None:
        a = _candidate(corridor_id=1, learned=0.8, length=10)
        b = _candidate(corridor_id=2, learned=0.8, length=5)
        ordered = sorted([a, b], key=_tie_break_key)
        assert ordered[0].corridor_id == 2

    def test_same_score_same_length_lower_id_wins(self) -> None:
        a = _candidate(corridor_id=7, learned=0.8, length=5)
        b = _candidate(corridor_id=3, learned=0.8, length=5)
        ordered = sorted([a, b], key=_tie_break_key)
        assert ordered[0].corridor_id == 3


# ---------------------------------------------------------------------
# Reject-reason branches
# ---------------------------------------------------------------------

class TestAssemblyRejects:
    def test_too_few_anchors(self) -> None:
        result = assemble_route_from_inputs(AssemblyInputs(
            map_id=1, is_linked_cp=True,
            anchors=(_anchor("Spawn", 0),),
            candidates=(),
        ))
        assert isinstance(result, AssemblyError)
        assert result.reason == "empty_corridors"
        assert "Spawn + Goal" in result.detail

    def test_plain_cp_short_circuits(self) -> None:
        result = assemble_route_from_inputs(AssemblyInputs(
            map_id=1, is_linked_cp=False,
            anchors=(_anchor("Spawn", 0), _anchor("Goal", 0)),
            candidates=(_candidate(),),
        ))
        assert isinstance(result, AssemblyError)
        assert result.reason == "plain_cp_not_supported_v0"

    def test_no_candidates_is_empty_corridors(self) -> None:
        result = assemble_route_from_inputs(AssemblyInputs(
            map_id=1, is_linked_cp=True,
            anchors=(_anchor("Spawn", 0), _anchor("Goal", 0)),
            candidates=(),
        ))
        assert isinstance(result, AssemblyError)
        assert result.reason == "empty_corridors"

    def test_missing_corridor_in_interval(self) -> None:
        # Two intervals; only first has a candidate.
        anchors = (
            _anchor("Spawn", 0, (0, 0, 0)),
            _anchor("Checkpoint", 1, (0, 0, 5)),
            _anchor("Goal", 0, (0, 0, 10)),
        )
        c1 = _candidate(
            corridor_id=1,
            src=anchors[0], dst=anchors[1],
            cells=((0, 0, 0), (0, 0, 5)),
        )
        result = assemble_route_from_inputs(AssemblyInputs(
            map_id=1, is_linked_cp=True,
            anchors=anchors,
            candidates=(c1,),
        ))
        assert isinstance(result, AssemblyError)
        assert result.reason == "missing_corridor_in_interval"
        assert result.interval_index == 1

    def test_unscored_corridor_treated_as_missing(self) -> None:
        # Candidate exists but learned_score is NULL → filtered out.
        anchors = (_anchor("Spawn", 0), _anchor("Goal", 0))
        c = _candidate(src=anchors[0], dst=anchors[1], learned=None)
        result = assemble_route_from_inputs(AssemblyInputs(
            map_id=1, is_linked_cp=True,
            anchors=anchors, candidates=(c,),
        ))
        assert isinstance(result, AssemblyError)
        assert result.reason == "missing_corridor_in_interval"

    def test_chain_broken_when_cells_discontinuous(self) -> None:
        anchors = (
            _anchor("Spawn", 0, (0, 0, 0)),
            _anchor("Checkpoint", 1, (0, 0, 5)),
            _anchor("Goal", 0, (9, 9, 9)),
        )
        c1 = _candidate(
            corridor_id=1,
            src=anchors[0], dst=anchors[1],
            cells=((0, 0, 0), (0, 0, 5)),
        )
        # Next corridor starts far from where the first ended.
        c2 = _candidate(
            corridor_id=2,
            src=anchors[1], dst=anchors[2],
            cells=((9, 9, 9),),
        )
        result = assemble_route_from_inputs(AssemblyInputs(
            map_id=1, is_linked_cp=True,
            anchors=anchors,
            candidates=(c1, c2),
        ))
        assert isinstance(result, AssemblyError)
        assert result.reason == "chain_broken"
        assert result.interval_index == 0

    def test_empty_path_cells_is_schema_invalid(self) -> None:
        anchors = (
            _anchor("Spawn", 0, (0, 0, 0)),
            _anchor("Checkpoint", 1, (0, 0, 1)),
            _anchor("Goal", 0, (0, 0, 2)),
        )
        c1 = _candidate(
            corridor_id=1,
            src=anchors[0], dst=anchors[1],
            cells=(),  # empty → invalid_schema
            length=0,
        )
        c2 = _candidate(
            corridor_id=2,
            src=anchors[1], dst=anchors[2],
            cells=((0, 0, 1), (0, 0, 2)),
        )
        result = assemble_route_from_inputs(AssemblyInputs(
            map_id=1, is_linked_cp=True,
            anchors=anchors,
            candidates=(c1, c2),
        ))
        assert isinstance(result, AssemblyError)
        assert result.reason == "invalid_schema"


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------

class TestAssemblyHappyPath:
    def _three_interval_inputs(self) -> AssemblyInputs:
        a = (
            _anchor("Spawn", 0, (0, 0, 0)),
            _anchor("Checkpoint", 1, (0, 0, 3)),
            _anchor("Checkpoint", 2, (0, 0, 6)),
            _anchor("Goal", 0, (0, 0, 9)),
        )
        c1 = _candidate(
            corridor_id=10, src=a[0], dst=a[1],
            cells=((0, 0, 0), (0, 0, 1), (0, 0, 2), (0, 0, 3)),
            learned=0.8,
        )
        c2 = _candidate(
            corridor_id=11, src=a[1], dst=a[2],
            cells=((0, 0, 3), (0, 0, 4), (0, 0, 5), (0, 0, 6)),
            learned=0.7,
        )
        c3 = _candidate(
            corridor_id=12, src=a[2], dst=a[3],
            cells=((0, 0, 6), (0, 0, 7), (0, 0, 8), (0, 0, 9)),
            learned=0.6,
        )
        return AssemblyInputs(
            map_id=42, is_linked_cp=True,
            anchors=a, candidates=(c1, c2, c3),
        )

    def test_happy_three_intervals(self) -> None:
        result = assemble_route_from_inputs(self._three_interval_inputs())
        assert isinstance(result, AssembledRoute)
        assert result.map_id == 42
        assert len(result.intervals) == 3
        assert [iv.chosen.corridor_id for iv in result.intervals] == [10, 11, 12]
        # cells_total = 4 + 4 + 4 = 12
        assert result.cells_total == 12
        # ai_confidence = (0.8 + 0.7 + 0.6) / 3 = 0.7
        assert result.ai_confidence == pytest.approx(0.7)
        # Scope-v0 sums per-corridor rounded expected times (not one
        # monolithic calculation). Per-corridor 4×32/30×1000 = 4266.67 →
        # rounds to 4267 × 3 = 12801. Honest rounding residual.
        assert result.estimated_time_ms == 12801

    def test_picks_top_score_per_interval(self) -> None:
        inputs = self._three_interval_inputs()
        # Inject a better alternative for interval 1.
        extra = _candidate(
            corridor_id=99, src=inputs.anchors[1], dst=inputs.anchors[2],
            cells=((0, 0, 3), (0, 0, 4), (0, 0, 5), (0, 0, 6)),
            learned=0.95,   # beats the 0.7 baseline
        )
        inputs2 = AssemblyInputs(
            map_id=inputs.map_id, is_linked_cp=inputs.is_linked_cp,
            anchors=inputs.anchors,
            candidates=tuple(inputs.candidates) + (extra,),
        )
        result = assemble_route_from_inputs(inputs2)
        assert isinstance(result, AssembledRoute)
        assert result.intervals[1].chosen.corridor_id == 99

    def test_single_interval_works(self) -> None:
        a = (_anchor("Spawn", 0, (0, 0, 0)), _anchor("Goal", 0, (0, 0, 3)))
        c = _candidate(
            corridor_id=5, src=a[0], dst=a[1],
            cells=((0, 0, 0), (0, 0, 3)),
            length=4,
            learned=0.9,
        )
        result = assemble_route_from_inputs(AssemblyInputs(
            map_id=1, is_linked_cp=True, anchors=a, candidates=(c,),
        ))
        assert isinstance(result, AssembledRoute)
        assert len(result.intervals) == 1
        assert result.ai_confidence == pytest.approx(0.9)

    def test_shared_anchor_cell_treated_as_continuous(self) -> None:
        # Two corridors that literally share a cell — not adjacent by
        # Chebyshev, but continuous via the shared-anchor clause.
        a = (
            _anchor("Spawn", 0, (0, 0, 0)),
            _anchor("Checkpoint", 1, (5, 5, 5)),  # anchor cell
            _anchor("Goal", 0, (0, 0, 9)),
        )
        c1 = _candidate(
            corridor_id=1, src=a[0], dst=a[1],
            cells=((0, 0, 0), (5, 5, 5)),  # jumps via last-cell == CP
        )
        c2 = _candidate(
            corridor_id=2, src=a[1], dst=a[2],
            cells=((5, 5, 5), (0, 0, 9)),  # starts at same CP cell
        )
        result = assemble_route_from_inputs(AssemblyInputs(
            map_id=1, is_linked_cp=True, anchors=a, candidates=(c1, c2),
        ))
        # Shared anchor → distance 0 → continuous.
        assert isinstance(result, AssembledRoute)


# ---------------------------------------------------------------------
# _detect_and_order_anchors — LinkedCheckpoint tag + multi-cell dedupe
# ---------------------------------------------------------------------

class TestDetectAndOrderAnchors:
    def test_linked_checkpoint_tag_triggers_linked(self) -> None:
        from src.generation.assembly import _detect_and_order_anchors
        rows = [
            (0, 0, "Spawn",            0, 0, 0),
            (1, 1, "LinkedCheckpoint", 1, 0, 1),
            (2, 0, "Goal",             2, 0, 2),
        ]
        linked, anchors = _detect_and_order_anchors(rows)
        assert linked is True
        assert [(a.tag, a.order) for a in anchors] == [
            ("Spawn", 0), ("LinkedCheckpoint", 1), ("Goal", 0),
        ]

    def test_plain_checkpoint_is_never_linked(self) -> None:
        # waypoint_order >= 1 on a plain ``Checkpoint`` row (parse
        # oddity) must NOT trigger Linked-CP — the tag is the
        # discriminator, not the order.
        from src.generation.assembly import _detect_and_order_anchors
        rows = [
            (0, 0, "Spawn",      0, 0, 0),
            (1, 1, "Checkpoint", 1, 0, 1),
            (2, 2, "Checkpoint", 2, 0, 2),
            (3, 0, "Goal",       3, 0, 3),
        ]
        linked, _anchors = _detect_and_order_anchors(rows)
        assert linked is False

    def test_mixed_tag_falls_back_to_plain(self) -> None:
        from src.generation.assembly import _detect_and_order_anchors
        rows = [
            (0, 0, "Spawn",            0, 0, 0),
            (1, 0, "Checkpoint",       1, 0, 1),
            (2, 1, "LinkedCheckpoint", 2, 0, 2),
            (3, 0, "Goal",             3, 0, 3),
        ]
        linked, _anchors = _detect_and_order_anchors(rows)
        assert linked is False

    def test_multi_cell_linked_cp_deduped(self) -> None:
        # Real-corpus map 1212 shape: each LinkedCheckpoint spans 2
        # cells, one DB row per cell, all sharing (tag, waypoint_order).
        # Before dedup, the chain would contain (LCP,1),(LCP,1) and the
        # assembler would look for a self-interval that doesn't exist.
        from src.generation.assembly import _detect_and_order_anchors
        rows = [
            (0, 0, "Spawn",            0, 0, 0),
            (1, 1, "LinkedCheckpoint", 1, 0, 1),   # order=1, cell A
            (2, 1, "LinkedCheckpoint", 2, 0, 1),   # order=1, cell B (same logical CP)
            (3, 2, "LinkedCheckpoint", 3, 0, 2),   # order=2, cell A
            (4, 2, "LinkedCheckpoint", 4, 0, 2),   # order=2, cell B
            (5, 0, "Goal",             5, 0, 3),
        ]
        linked, anchors = _detect_and_order_anchors(rows)
        assert linked is True
        # 4 logical anchors (Spawn + 2 LCPs + Goal), not 6.
        assert [(a.tag, a.order) for a in anchors] == [
            ("Spawn", 0),
            ("LinkedCheckpoint", 1),
            ("LinkedCheckpoint", 2),
            ("Goal", 0),
        ]

    def test_goal_missing_stays_plain(self) -> None:
        # No Goal row → can't close Spawn→Goal → not linked.
        from src.generation.assembly import _detect_and_order_anchors
        rows = [
            (0, 0, "Spawn",            0, 0, 0),
            (1, 1, "LinkedCheckpoint", 1, 0, 1),
        ]
        linked, _anchors = _detect_and_order_anchors(rows)
        assert linked is False
