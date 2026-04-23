"""Tests for corridor path enumeration + §8.3 / §8.4 gates.

Pure-function tests for the graph construction, path enumeration,
sanity-check evaluation, and stability perturbation. DB-touching
``enumerate_map`` / ``enumerate_set`` are exercised via integration
once a fixture exists.
"""
from __future__ import annotations

import pytest

from src.corridor.traversability.enumeration import (
    DECO_ADJACENT_CONTAMINATION_CAP,
    MEDIAN_PATH_COUNT_CAP,
    P95_PATH_COUNT_CAP,
    EnumerationReport,
    IntervalEnumeration,
    _build_enumeration_graph,
    _compute_deco_adjacent_contamination,
    _enumerate_simple_paths,
    _evaluate_corridor_sanity,
    _plan_intervals,
    _top_ranked_path,
)
from src.corridor.traversability.reachability import AnchorSet


class TestEnumerateSimplePaths:
    def test_source_equals_target_returns_single_cell_path(self) -> None:
        nb: dict = {}
        paths = _enumerate_simple_paths(
            nb, frozenset({(0, 0, 0)}), frozenset({(0, 0, 0)}), depth_cap=5,
        )
        assert paths == [[(0, 0, 0)]]

    def test_finds_direct_neighbor(self) -> None:
        nb = {(0, 0, 0): [(1, 0, 0)], (1, 0, 0): [(0, 0, 0)]}
        paths = _enumerate_simple_paths(
            nb, frozenset({(0, 0, 0)}), frozenset({(1, 0, 0)}), depth_cap=5,
        )
        assert [(0, 0, 0), (1, 0, 0)] in paths

    def test_depth_cap_respected(self) -> None:
        # Chain 0→1→2→3→4; depth_cap=2 limits reach to 2 steps away.
        chain = {
            (0, 0, 0): [(1, 0, 0)],
            (1, 0, 0): [(0, 0, 0), (2, 0, 0)],
            (2, 0, 0): [(1, 0, 0), (3, 0, 0)],
            (3, 0, 0): [(2, 0, 0), (4, 0, 0)],
            (4, 0, 0): [(3, 0, 0)],
        }
        paths = _enumerate_simple_paths(
            chain, frozenset({(0, 0, 0)}), frozenset({(4, 0, 0)}), depth_cap=2,
        )
        assert paths == []

    def test_reaches_within_cap(self) -> None:
        chain = {
            (0, 0, 0): [(1, 0, 0)],
            (1, 0, 0): [(0, 0, 0), (2, 0, 0)],
            (2, 0, 0): [(1, 0, 0)],
        }
        paths = _enumerate_simple_paths(
            chain, frozenset({(0, 0, 0)}), frozenset({(2, 0, 0)}), depth_cap=5,
        )
        assert [(0, 0, 0), (1, 0, 0), (2, 0, 0)] in paths

    def test_hard_cap_short_circuits(self) -> None:
        # A complete graph of 6 cells has many simple paths; hard_cap=3
        # should cut the enumeration off early.
        cells = [(i, 0, 0) for i in range(6)]
        nb = {c: [n for n in cells if n != c] for c in cells}
        paths = _enumerate_simple_paths(
            nb, frozenset({cells[0]}), frozenset({cells[-1]}),
            depth_cap=10, hard_cap=3,
        )
        assert len(paths) <= 3


class TestBuildEnumerationGraph:
    def test_empty_observations_returns_seed_only(self) -> None:
        seed = {(0, 0, 0): [(1, 0, 0)], (1, 0, 0): [(0, 0, 0)]}
        combined, virtual = _build_enumeration_graph(seed, [])
        assert virtual == set()
        assert (1, 0, 0) in combined[(0, 0, 0)]

    def test_observation_adds_virtual_edges(self) -> None:
        obs = frozenset({(0, 0, 0), (5, 0, 0)})
        combined, virtual = _build_enumeration_graph({}, [obs])
        assert ((0, 0, 0), (5, 0, 0)) in virtual
        assert (5, 0, 0) in combined[(0, 0, 0)]
        assert (0, 0, 0) in combined[(5, 0, 0)]

    def test_multi_cell_observation_creates_pairwise(self) -> None:
        obs = frozenset({(0, 0, 0), (1, 0, 0), (2, 0, 0)})
        _, virtual = _build_enumeration_graph({}, [obs])
        # 3 cells → 3 pairs
        assert len(virtual) == 3


class TestComputeDecoAdjacentContamination:
    def test_no_interior_cells_returns_zero(self) -> None:
        anchors = frozenset({(0, 0, 0), (1, 0, 0)})
        corridor = anchors  # corridor = anchors only (empty interior)
        assert _compute_deco_adjacent_contamination(
            corridor, anchors, {},
        ) == 0.0

    def test_all_interior_deco_adjacent_returns_one(self) -> None:
        anchors = frozenset({(0, 0, 0), (10, 0, 0)})
        corridor = frozenset({(0, 0, 0), (5, 0, 0), (10, 0, 0)})
        fam = {
            (5, 0, 0): "Platform",  # corridor interior
            (5, 1, 0): "Deco",       # deco neighbor
            (0, 0, 0): "Road",
            (10, 0, 0): "Road",
        }
        assert _compute_deco_adjacent_contamination(corridor, anchors, fam) == 1.0

    def test_anchor_neighbors_do_not_count(self) -> None:
        # Anchor (0,0,0) has Deco neighbor but is excluded from
        # contamination; there are NO interior cells.
        anchors = frozenset({(0, 0, 0)})
        corridor = anchors
        fam = {(0, 0, 0): "Road", (0, 1, 0): "Deco"}
        assert _compute_deco_adjacent_contamination(corridor, anchors, fam) == 0.0


class TestEvaluateCorridorSanity:
    def test_empty_paths_result_empty(self) -> None:
        iv = IntervalEnumeration(
            map_id=1, src_tag="Spawn", src_order=0,
            dst_tag="Goal", dst_order=0,
        )
        _evaluate_corridor_sanity(
            paths=[],
            cell_to_family={},
            virtual_edges=set(),
            anchor_cells=frozenset(),
            iv=iv,
        )
        assert iv.corridor_cells == frozenset()
        assert iv.unsupported_edges_in_corridors == 0
        assert iv.non_drivable_cells_in_corridors == 0

    def test_drivable_path_has_no_contamination(self) -> None:
        iv = IntervalEnumeration(
            map_id=1, src_tag="Spawn", src_order=0,
            dst_tag="Goal", dst_order=0,
        )
        _evaluate_corridor_sanity(
            paths=[[(0, 0, 0), (1, 0, 0), (2, 0, 0)]],
            cell_to_family={
                (0, 0, 0): "Road", (1, 0, 0): "Platform", (2, 0, 0): "Road",
            },
            virtual_edges=set(),
            anchor_cells=frozenset({(0, 0, 0), (2, 0, 0)}),
            iv=iv,
        )
        assert iv.passes_sanity_1_unsupported
        assert iv.passes_sanity_2_non_drivable
        assert iv.passes_sanity_3_deco_adjacent

    def test_non_drivable_path_cell_flagged(self) -> None:
        iv = IntervalEnumeration(
            map_id=1, src_tag="Spawn", src_order=0,
            dst_tag="Goal", dst_order=0,
        )
        _evaluate_corridor_sanity(
            paths=[[(0, 0, 0), (1, 0, 0)]],
            cell_to_family={
                (0, 0, 0): "Road", (1, 0, 0): "Deco",
            },
            virtual_edges=set(),
            anchor_cells=frozenset({(0, 0, 0)}),
            iv=iv,
        )
        assert iv.non_drivable_cells_in_corridors == 1
        assert not iv.passes_sanity_2_non_drivable

    def test_virtual_edge_skipped_in_unsupported_check(self) -> None:
        iv = IntervalEnumeration(
            map_id=1, src_tag="Spawn", src_order=0,
            dst_tag="Goal", dst_order=0,
        )
        # (0,0,0) → (5,0,0) via a virtual edge — no intermediate cells.
        # unsupported-edge check should ignore this because virtual edges
        # are connectivity assertions, not block-level edges.
        _evaluate_corridor_sanity(
            paths=[[(0, 0, 0), (5, 0, 0)]],
            cell_to_family={
                (0, 0, 0): "Road", (5, 0, 0): "Deco",   # deco BUT virtual
            },
            virtual_edges={((0, 0, 0), (5, 0, 0))},
            anchor_cells=frozenset({(0, 0, 0), (5, 0, 0)}),
            iv=iv,
        )
        # The virtual edge is skipped from unsupported-edge check;
        # but the (5,0,0) Deco cell is still flagged at the cell level.
        assert iv.unsupported_edges_in_corridors == 0
        assert iv.non_drivable_cells_in_corridors == 1


class TestTopRankedPath:
    def test_empty_paths_returns_none(self) -> None:
        assert _top_ranked_path([]) is None

    def test_shortest_path_wins(self) -> None:
        long = [(0, 0, 0), (1, 0, 0), (2, 0, 0)]
        short = [(0, 0, 0), (5, 0, 0)]
        top = _top_ranked_path([long, short])
        assert top == tuple(tuple(c) for c in short)

    def test_ties_broken_lexicographically(self) -> None:
        # Both paths have length 2. Path "a" < path "b" by tuple
        # comparison.
        a = [(0, 0, 0), (0, 1, 0)]
        b = [(0, 0, 0), (1, 0, 0)]
        # "a" tuple sequence is smaller than "b" tuple sequence
        assert _top_ranked_path([b, a]) == tuple(tuple(c) for c in a)


class TestEnumerationReportGates:
    def _mk(self, path_counts: list[int], sanity_all_pass: bool = True) -> EnumerationReport:
        r = EnumerationReport()
        intervals = []
        for i, pc in enumerate(path_counts):
            iv = IntervalEnumeration(
                map_id=1, src_tag="Spawn", src_order=0,
                dst_tag="Goal", dst_order=i,
            )
            iv.path_count = pc
            if not sanity_all_pass:
                iv.non_drivable_cells_in_corridors = 1  # fails sanity #2
            intervals.append(iv)
        r.per_map[1] = intervals
        return r

    def test_empty_report_medians_zero(self) -> None:
        r = EnumerationReport()
        assert r.median_path_count == 0
        assert r.p95_path_count == 0

    def test_tractability_gate_passes(self) -> None:
        r = self._mk([1, 5, 10, 50, 100])
        assert r.passes_84_median
        assert r.passes_84_p95

    def test_tractability_gate_fails_on_explosion(self) -> None:
        r = self._mk([1, 5, 10] + [P95_PATH_COUNT_CAP + 1] * 2)
        assert r.passes_84_median  # median still small
        assert not r.passes_84_p95  # p95 blows up

    def test_sanity_gate_detects_non_drivable(self) -> None:
        r = self._mk([1], sanity_all_pass=False)
        assert not r.passes_83_non_drivable


class TestIntervalEnumerationSanityFlags:
    def test_sanity_4_none_counts_as_pass(self) -> None:
        # None = not assessed — should not block the gate. The design
        # note treats unassessable intervals as "no evidence of
        # instability" rather than as failing.
        iv = IntervalEnumeration(
            map_id=1, src_tag="Spawn", src_order=0,
            dst_tag="Goal", dst_order=0,
            top_corridor_stable=None,
        )
        assert iv.passes_sanity_4_stable

    def test_sanity_4_false_blocks_gate(self) -> None:
        iv = IntervalEnumeration(
            map_id=1, src_tag="Spawn", src_order=0,
            dst_tag="Goal", dst_order=0,
            top_corridor_stable=False,
        )
        assert not iv.passes_sanity_4_stable


# ---------------------------------------------------------------------
# _plan_intervals — interval-shape rule (plain-CP vs Linked-CP)
# ---------------------------------------------------------------------

class TestPlanIntervals:
    def _aset(self, tag: str, order: int, *cells: tuple[int, int, int]) -> AnchorSet:
        return AnchorSet(tag=tag, waypoint_order=order, cells=frozenset(cells))

    def test_plain_cp_emits_spawn_to_each(self) -> None:
        # Two plain CPs (order=0) + Goal. Phase-1 shape: Spawn → each.
        plans = _plan_intervals([
            self._aset("Spawn",      0, (0, 0, 0)),
            self._aset("Checkpoint", 0, (1, 0, 0)),
            self._aset("Checkpoint", 0, (2, 0, 0)),
            self._aset("Goal",       0, (3, 0, 0)),
        ])
        assert len(plans) == 3
        for p in plans:
            assert (p.src_tag, p.src_order) == ("Spawn", 0)
            assert p.sources == frozenset({(0, 0, 0)})
        dsts = {(p.dst_tag, p.dst_order) for p in plans}
        assert dsts == {("Checkpoint", 0), ("Goal", 0)}

    def test_linked_cp_emits_chain(self) -> None:
        # Spawn + 3 LinkedCheckpoints + Goal → 4 chain intervals.
        plans = _plan_intervals([
            self._aset("Spawn",            0, (0, 0, 0)),
            self._aset("LinkedCheckpoint", 2, (0, 0, 2)),
            self._aset("LinkedCheckpoint", 1, (0, 0, 1)),
            self._aset("LinkedCheckpoint", 3, (0, 0, 3)),
            self._aset("Goal",             0, (0, 0, 4)),
        ])
        assert [(p.src_tag, p.src_order, p.dst_tag, p.dst_order) for p in plans] == [
            ("Spawn",            0, "LinkedCheckpoint", 1),
            ("LinkedCheckpoint", 1, "LinkedCheckpoint", 2),
            ("LinkedCheckpoint", 2, "LinkedCheckpoint", 3),
            ("LinkedCheckpoint", 3, "Goal",             0),
        ]
        # Every CP→CP interval's targets are the next CP's cells.
        assert plans[1].sources == frozenset({(0, 0, 1)})
        assert plans[1].targets == frozenset({(0, 0, 2)})

    def test_plain_checkpoint_tag_is_never_linked(self) -> None:
        # Non-zero waypoint_order on a plain ``Checkpoint`` tag (not
        # ``LinkedCheckpoint``) does NOT trigger chain enumeration.
        # The parser-audited corpus showed this shape on parse-defective
        # maps (539 / 2637 / etc.) — we must not misread it as Linked-CP
        # and clobber the plain-CP shape the 514-map training used.
        plans = _plan_intervals([
            self._aset("Spawn",      0, (0, 0, 0)),
            self._aset("Checkpoint", 1, (0, 0, 1)),
            self._aset("Checkpoint", 2, (0, 0, 2)),
            self._aset("Goal",       0, (0, 0, 3)),
        ])
        assert all((p.src_tag, p.src_order) == ("Spawn", 0) for p in plans)

    def test_mixed_checkpoint_and_linked_checkpoint_falls_back(self) -> None:
        # Defensive: maps with both ``Checkpoint`` and ``LinkedCheckpoint``
        # rows (observed in 5+ corpus maps) fall back to plain. The
        # generator's assembler will reject them downstream anyway; we
        # just avoid emitting a partial / nonsensical chain here.
        plans = _plan_intervals([
            self._aset("Spawn",            0, (0, 0, 0)),
            self._aset("Checkpoint",       0, (0, 0, 1)),
            self._aset("LinkedCheckpoint", 1, (0, 0, 2)),
            self._aset("Goal",             0, (0, 0, 3)),
        ])
        assert all((p.src_tag, p.src_order) == ("Spawn", 0) for p in plans)

    def test_linked_cp_without_goal_falls_back_to_plain(self) -> None:
        # Goal missing — treat as plain so Phase-1 training stays
        # invariant. The generator still rejects downstream.
        plans = _plan_intervals([
            self._aset("Spawn",            0, (0, 0, 0)),
            self._aset("LinkedCheckpoint", 1, (0, 0, 1)),
            self._aset("LinkedCheckpoint", 2, (0, 0, 2)),
        ])
        assert all((p.src_tag, p.src_order) == ("Spawn", 0) for p in plans)

    def test_empty_when_no_spawn(self) -> None:
        # Pathological: waypoints without a Spawn. Matches enumerate_map's
        # historical guard (no sources → nothing to enumerate).
        plans = _plan_intervals([
            self._aset("LinkedCheckpoint", 1, (0, 0, 1)),
            self._aset("Goal",             0, (0, 0, 2)),
        ])
        assert plans == []

    def test_spawn_cells_union_across_multiple_spawn_sets(self) -> None:
        # StartFinish + Spawn both count as spawn. Plain-CP baseline
        # uses the union of all spawn cells as sources.
        plans = _plan_intervals([
            self._aset("Spawn",       0, (0, 0, 0)),
            self._aset("StartFinish", 0, (9, 0, 0)),
            self._aset("Goal",        0, (5, 0, 0)),
        ])
        assert len(plans) == 1
        assert plans[0].sources == frozenset({(0, 0, 0), (9, 0, 0)})
