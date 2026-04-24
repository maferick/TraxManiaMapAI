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


class TestValidationTieBreak:
    """#226/#227 validator wired into assembly ranking.

    Learned_corridor_score stays authoritative; validation injects
    tiers *after* it. Verified invariants:
      - higher learned score always wins, regardless of validation
      - within equal learned buckets, validation_score breaks ties
      - validation_score == 0 is pushed to the back of the bucket
      - None validation_score degrades gracefully (no crash, no swap
        bias vs. un-validated siblings)
    """

    def _cand(self, cid: int, learned: float, val: float | None) -> CandidateCorridor:
        from src.generation.assembly import CandidateCorridor as CC
        return CC(
            corridor_id=cid, map_id=42,
            src=_anchor("Spawn", 0), dst=_anchor("Goal", 0),
            path_cells=((0, 0, 0), (0, 0, 1)),
            path_length=2, contains_virtual_edge=False,
            corridor_confidence=0.5,
            learned_corridor_score=learned,
            validation_score=val,
        )

    def test_learned_wins_regardless_of_validation(self) -> None:
        # Low learned + high validation loses to high learned + 0 validation.
        # The trained ranker stays authoritative.
        low_learned_high_val = self._cand(1, learned=0.5, val=1.0)
        high_learned_zero_val = self._cand(2, learned=0.9, val=0.0)
        from src.generation.assembly import _tie_break_key_with_validation
        ordered = sorted(
            [low_learned_high_val, high_learned_zero_val],
            key=_tie_break_key_with_validation,
        )
        assert ordered[0].corridor_id == 2  # learned=0.9 wins

    def test_validation_breaks_learned_ties(self) -> None:
        # Equal learned, different validation. Higher validation wins.
        from src.generation.assembly import _tie_break_key_with_validation
        a = self._cand(1, learned=0.8, val=0.3)
        b = self._cand(2, learned=0.8, val=0.9)
        ordered = sorted([a, b], key=_tie_break_key_with_validation)
        assert ordered[0].corridor_id == 2

    def test_zero_validation_sorts_to_back_of_bucket(self) -> None:
        # Equal learned, one validation=0.0 and one validation=0.1 →
        # the 0.0 goes to the back; 'avoid validation_score = 0' rule.
        from src.generation.assembly import _tie_break_key_with_validation
        zero_val = self._cand(1, learned=0.8, val=0.0)
        tiny_val = self._cand(2, learned=0.8, val=0.1)
        ordered = sorted(
            [zero_val, tiny_val], key=_tie_break_key_with_validation,
        )
        assert ordered[0].corridor_id == 2
        assert ordered[-1].corridor_id == 1

    def test_none_validation_treated_as_neutral(self) -> None:
        # None validation treated as -1 in the score tier but NOT
        # in the zero-floor tier — so a None corridor out-ranks a
        # zero-score corridor within the same learned bucket, which
        # is the right answer: we shouldn't penalise un-validated
        # corridors as if they were fully-broken ones.
        from src.generation.assembly import _tie_break_key_with_validation
        zero_val = self._cand(1, learned=0.8, val=0.0)
        none_val = self._cand(2, learned=0.8, val=None)
        ordered = sorted(
            [zero_val, none_val], key=_tie_break_key_with_validation,
        )
        assert ordered[0].corridor_id == 2

    def test_opt_out_preserves_legacy_behaviour(self) -> None:
        # When use_validation_tie_break=False, the assembly result
        # must match the pre-validator output exactly — even with
        # validation scores populated on the candidates.
        src, dst = _anchor("Spawn", 0), _anchor("Goal", 0)
        scored = self._cand(1, learned=0.5, val=0.9)
        unscored = self._cand(2, learned=0.9, val=0.0)
        # Make src/dst match both
        for c in (scored, unscored):
            pass  # already share the _anchor default
        inputs = AssemblyInputs(
            map_id=42, is_linked_cp=True,
            anchors=(src, dst),
            candidates=(scored, unscored),
            use_validation_tie_break=False,
        )
        result = assemble_route_from_inputs(inputs)
        from src.generation import AssembledRoute
        assert isinstance(result, AssembledRoute)
        # Without validator participation, learned=0.9 wins.
        assert result.intervals[0].chosen.corridor_id == 2

    def test_validator_swap_flips_pick(self) -> None:
        # With validator participation, a zero-validation leader
        # flips to a lower-learned alternative *only* within the
        # same learned-score bucket. Here both are equal learned
        # → validation breaks the tie.
        src, dst = _anchor("Spawn", 0), _anchor("Goal", 0)
        clean = self._cand(9, learned=0.7, val=0.95)
        broken = self._cand(1, learned=0.7, val=0.0)
        inputs = AssemblyInputs(
            map_id=42, is_linked_cp=True,
            anchors=(src, dst),
            candidates=(clean, broken),
            use_validation_tie_break=True,
        )
        result = assemble_route_from_inputs(inputs)
        from src.generation import AssembledRoute
        assert isinstance(result, AssembledRoute)
        assert result.intervals[0].chosen.corridor_id == 9
        assert result.intervals[0].chosen.validation_score == 0.95


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

    # Note: the historic `test_chain_broken_when_cells_discontinuous`
    # was removed in Level-1 mutation. Scope-v0's chain-continuity
    # rule is "adjacent OR share an anchor block"; the old test
    # constructed corridors that shared the bridging anchor by
    # construction (same (tag, order) on both sides of the join), so
    # it was only exercising the cell-adjacency sub-clause and the
    # scope-doc-correct "shared anchor" clause now covers it. The
    # `chain_broken` reject_reason remains in the enum as a tripwire
    # for future modes (non-Linked-CP, data corruption) but is
    # unreachable from well-formed Linked-CP assemble-from-inputs
    # today. The positive-coverage test for the shared-anchor clause
    # is `test_multi_cell_anchor_treated_as_continuous` and
    # `test_shared_anchor_cell_treated_as_continuous` under
    # `TestAssemblyHappy`.

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

    def test_multi_cell_anchor_treated_as_continuous(self) -> None:
        # Real-corpus case (map 1212): LinkedCheckpoint #3 has two
        # cells (27, 11, 33) and (20, 11, 33) — not Chebyshev-adjacent.
        # Corridor i ends at one; corridor i+1 starts at the other.
        # scope-v0 "share an anchor block" clause must cover this or
        # Level-1 mutation will spuriously reject chain_broken.
        a = (
            _anchor("Spawn", 0, (0, 0, 0)),
            _anchor("LinkedCheckpoint", 3, (27, 11, 33)),  # rep cell
            _anchor("Goal", 0, (40, 11, 33)),
        )
        c1 = _candidate(
            corridor_id=1, src=a[0], dst=a[1],
            cells=((0, 0, 0), (27, 11, 33)),  # lands on cell A of LCP#3
        )
        c2 = _candidate(
            corridor_id=2, src=a[1], dst=a[2],
            cells=((20, 11, 33), (40, 11, 33)),  # starts on cell B of LCP#3
        )
        result = assemble_route_from_inputs(AssemblyInputs(
            map_id=1, is_linked_cp=True, anchors=a, candidates=(c1, c2),
        ))
        # Shared anchor clause → continuous even though cells are
        # 7 apart on x.
        assert isinstance(result, AssembledRoute)

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


# ---------------------------------------------------------------------
# Level-1 mutation — seed-driven pick-within-top-K
# ---------------------------------------------------------------------

class TestPickWithinTopK:
    def test_pool_of_one_always_picks_zero(self) -> None:
        from src.generation.assembly import _pick_within_top_k
        for seed in (0, 1, 42, 99999):
            idx = _pick_within_top_k(
                random_seed=seed, interval_index=0,
                pool_size=1, top_k=3,
            )
            assert idx == 0

    def test_top_k_one_always_picks_zero(self) -> None:
        # Legacy/default top_k=1 must always select rank-1, regardless
        # of seed — guarantees Phase-1 behaviour is the unchanged path.
        from src.generation.assembly import _pick_within_top_k
        for seed in (0, 1, 42, 99999):
            for idx_i in range(10):
                idx = _pick_within_top_k(
                    random_seed=seed, interval_index=idx_i,
                    pool_size=20, top_k=1,
                )
                assert idx == 0

    def test_deterministic_for_same_inputs(self) -> None:
        from src.generation.assembly import _pick_within_top_k
        a = _pick_within_top_k(
            random_seed=42, interval_index=3, pool_size=20, top_k=3,
        )
        b = _pick_within_top_k(
            random_seed=42, interval_index=3, pool_size=20, top_k=3,
        )
        assert a == b

    def test_picked_index_in_range(self) -> None:
        from src.generation.assembly import _pick_within_top_k
        for seed in range(50):
            idx = _pick_within_top_k(
                random_seed=seed, interval_index=0,
                pool_size=10, top_k=3,
            )
            assert 0 <= idx < 3

    def test_different_seeds_cover_multiple_ranks(self) -> None:
        # Over a spread of seeds, every rank in [0, k) should be hit —
        # otherwise the hash is degenerate and mutation isn't actually
        # happening.
        from src.generation.assembly import _pick_within_top_k
        picks = {
            _pick_within_top_k(
                random_seed=s, interval_index=0,
                pool_size=10, top_k=3,
            )
            for s in range(200)
        }
        assert picks == {0, 1, 2}


class TestMutationEndToEnd:
    def _candidates_for_interval(
        self, *, src, dst, count: int, base_id: int,
    ) -> tuple[CandidateCorridor, ...]:
        # N candidates, each a shade less good than the one before.
        return tuple(
            _candidate(
                corridor_id=base_id + i, src=src, dst=dst,
                cells=((0, 0, 0), (0, 0, i + 1)),
                length=10 + i,
                learned=0.9 - 0.05 * i,
            )
            for i in range(count)
        )

    def test_default_k1_picks_rank_one(self) -> None:
        # Default behaviour (top_k=1) picks the best-ranked corridor
        # regardless of seed — the Phase-1 contract survives.
        a = (_anchor("Spawn", 0, (0, 0, 0)),
             _anchor("Goal", 0, (0, 0, 1)))
        pool = self._candidates_for_interval(
            src=a[0], dst=a[1], count=5, base_id=100,
        )
        result_a = assemble_route_from_inputs(AssemblyInputs(
            map_id=1, is_linked_cp=True, anchors=a, candidates=pool,
            random_seed=0,
        ))
        result_b = assemble_route_from_inputs(AssemblyInputs(
            map_id=1, is_linked_cp=True, anchors=a, candidates=pool,
            random_seed=999,
        ))
        assert isinstance(result_a, AssembledRoute)
        assert isinstance(result_b, AssembledRoute)
        assert (result_a.intervals[0].chosen.corridor_id
                == result_b.intervals[0].chosen.corridor_id == 100)

    def test_top_k_three_produces_seed_variation(self) -> None:
        # With top_k=3 + 5 candidates, we should see at least two
        # different chosen corridors across different seeds.
        a = (_anchor("Spawn", 0, (0, 0, 0)),
             _anchor("Goal", 0, (0, 0, 1)))
        pool = self._candidates_for_interval(
            src=a[0], dst=a[1], count=5, base_id=200,
        )
        chosen = set()
        for s in range(50):
            r = assemble_route_from_inputs(AssemblyInputs(
                map_id=1, is_linked_cp=True, anchors=a, candidates=pool,
                random_seed=s, top_k_candidates=3,
            ))
            assert isinstance(r, AssembledRoute)
            chosen.add(r.intervals[0].chosen.corridor_id)
        # With pool_size=5, top_k=3 → corridor_ids 200/201/202.
        assert chosen == {200, 201, 202}

    def test_top_k_never_exceeds_pool_size(self) -> None:
        # 2 candidates but top_k=5 → picks come only from the 2
        # available, never index out of range.
        a = (_anchor("Spawn", 0, (0, 0, 0)),
             _anchor("Goal", 0, (0, 0, 1)))
        pool = self._candidates_for_interval(
            src=a[0], dst=a[1], count=2, base_id=300,
        )
        for s in range(30):
            r = assemble_route_from_inputs(AssemblyInputs(
                map_id=1, is_linked_cp=True, anchors=a, candidates=pool,
                random_seed=s, top_k_candidates=5,
            ))
            assert isinstance(r, AssembledRoute)
            assert r.intervals[0].chosen.corridor_id in (300, 301)

    def test_determinism_across_runs(self) -> None:
        # Same (inputs, seed, k) produces bit-identical route every
        # time — run_id reproducibility depends on this invariant.
        a = (_anchor("Spawn", 0, (0, 0, 0)),
             _anchor("Goal", 0, (0, 0, 1)))
        pool = self._candidates_for_interval(
            src=a[0], dst=a[1], count=7, base_id=400,
        )
        results = [
            assemble_route_from_inputs(AssemblyInputs(
                map_id=1, is_linked_cp=True, anchors=a, candidates=pool,
                random_seed=777, top_k_candidates=3,
            ))
            for _ in range(3)
        ]
        assert all(isinstance(r, AssembledRoute) for r in results)
        chosen_ids = {r.intervals[0].chosen.corridor_id for r in results}
        assert len(chosen_ids) == 1


# ---------------------------------------------------------------------
# #218-5 — combined_sequence_score as a tier-below tie-break
# ---------------------------------------------------------------------

class TestSequenceScoreTieBreak:
    def _two_candidates_same_learned(
        self, seq_a: float | None, seq_b: float | None,
    ) -> tuple[AssemblyInputs, CandidateCorridor, CandidateCorridor]:
        spawn = _anchor("Spawn", 0, (0, 0, 0))
        goal = _anchor("Goal", 0, (0, 0, 5))
        a = _candidate(
            corridor_id=100, src=spawn, dst=goal,
            cells=((0, 0, 0), (0, 0, 5)),
            learned=0.7,
        )
        b = _candidate(
            corridor_id=200, src=spawn, dst=goal,
            cells=((0, 0, 0), (0, 0, 5)),
            learned=0.7,
        )
        # Attach sequence scores via dataclasses.replace since the
        # helper doesn't take the kwarg.
        import dataclasses
        a = dataclasses.replace(a, combined_sequence_score=seq_a)
        b = dataclasses.replace(b, combined_sequence_score=seq_b)
        return AssemblyInputs(
            map_id=1, is_linked_cp=True,
            anchors=(spawn, goal), candidates=(a, b),
        ), a, b

    def test_higher_sequence_score_wins_on_learned_tie(self) -> None:
        inputs, a, b = self._two_candidates_same_learned(
            seq_a=0.4, seq_b=0.9,
        )
        result = assemble_route_from_inputs(inputs)
        assert isinstance(result, AssembledRoute)
        # b has the higher sequence score → b is chosen.
        assert result.intervals[0].chosen.corridor_id == 200
        assert result.intervals[0].chosen.combined_sequence_score == 0.9

    def test_null_sequence_loses_to_scored_sequence(self) -> None:
        # NULL (un-scored) corridor treated as -1 in the sort key
        # → loses to a scored corridor on learned-score ties.
        inputs, a, b = self._two_candidates_same_learned(
            seq_a=None, seq_b=0.1,
        )
        result = assemble_route_from_inputs(inputs)
        assert isinstance(result, AssembledRoute)
        assert result.intervals[0].chosen.corridor_id == 200

    def test_both_null_falls_through_to_path_length(self) -> None:
        # Both null → they tie on tier-2 as well, assembly falls
        # through to path_length → corridor_id.
        import dataclasses
        spawn = _anchor("Spawn", 0, (0, 0, 0))
        goal = _anchor("Goal", 0, (0, 0, 5))
        a = _candidate(
            corridor_id=100, src=spawn, dst=goal,
            cells=((0, 0, 0), (0, 0, 5)),
            length=10, learned=0.7,
        )
        b = _candidate(
            corridor_id=200, src=spawn, dst=goal,
            cells=((0, 0, 0), (0, 0, 5)),
            length=5, learned=0.7,
        )
        a = dataclasses.replace(a, combined_sequence_score=None)
        b = dataclasses.replace(b, combined_sequence_score=None)
        result = assemble_route_from_inputs(AssemblyInputs(
            map_id=1, is_linked_cp=True,
            anchors=(spawn, goal), candidates=(a, b),
        ))
        assert isinstance(result, AssembledRoute)
        # Shorter path wins.
        assert result.intervals[0].chosen.corridor_id == 200
