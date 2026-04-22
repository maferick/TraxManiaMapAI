"""Tests for the pure reachability helpers.

The DB-touching orchestration (`validate_map`, `validate_set`) is
exercised via integration once a fixture is available; unit tests
here cover the graph construction + BFS + anchor grouping in
isolation.
"""
from __future__ import annotations

from src.corridor.traversability.reachability import (
    AnchorSet,
    MapReachability,
    ReplayObservation,
    ValidationReport,
    _bfs_reachable,
    _build_anchor_sets,
    _build_cell_graph,
    _build_observations,
    _UnionFind,
)


class TestBuildCellGraph:
    def test_empty_input(self) -> None:
        cells, nb, counts = _build_cell_graph([])
        assert cells == {}
        assert dict(nb) == {}
        assert counts["seed_valid"] == 0
        assert counts["unsupported"] == 0

    def test_two_drivable_neighbors_become_seed_valid(self) -> None:
        rows = [
            (0, 0, 0, "Platform"),
            (1, 0, 0, "Platform"),
        ]
        _, nb, counts = _build_cell_graph(rows)
        assert counts["seed_valid"] == 1
        assert (1, 0, 0) in nb[(0, 0, 0)]
        assert (0, 0, 0) in nb[(1, 0, 0)]

    def test_deco_neighbor_is_unsupported_and_not_in_neighbors(self) -> None:
        rows = [
            (0, 0, 0, "Platform"),
            (1, 0, 0, "Deco"),
        ]
        _, nb, counts = _build_cell_graph(rows)
        assert counts["unsupported"] == 1
        assert counts["seed_valid"] == 0
        # Deco cell has no seed_valid neighbors from the Platform side
        assert (1, 0, 0) not in nb
        assert (0, 0, 0) not in nb

    def test_ambiguous_neighbor_is_unknown(self) -> None:
        rows = [
            (0, 0, 0, "Platform"),
            (1, 0, 0, "Open"),
        ]
        _, _, counts = _build_cell_graph(rows)
        assert counts["unknown"] == 1
        assert counts["seed_valid"] == 0

    def test_six_axis_neighbors_all_counted(self) -> None:
        center = (5, 5, 5)
        rows = [(*center, "Platform")]
        for off in [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]:
            rows.append((center[0] + off[0], center[1] + off[1], center[2] + off[2], "Platform"))
        _, nb, counts = _build_cell_graph(rows)
        assert counts["seed_valid"] == 6
        assert len(nb[center]) == 6

    def test_diagonal_neighbors_are_not_counted(self) -> None:
        rows = [
            (0, 0, 0, "Platform"),
            (1, 1, 0, "Platform"),   # diagonal, not axis
            (1, 0, 1, "Platform"),   # diagonal
        ]
        _, _, counts = _build_cell_graph(rows)
        # Only the 2 diagonal-pairs are present — neither is adjacent
        # by axis, nor are they adjacent to each other. Zero edges.
        assert counts["seed_valid"] == 0

    def test_each_edge_counted_once(self) -> None:
        # A chain of 3 Platform blocks: (0,0,0) — (1,0,0) — (2,0,0)
        rows = [
            (0, 0, 0, "Platform"),
            (1, 0, 0, "Platform"),
            (2, 0, 0, "Platform"),
        ]
        _, nb, counts = _build_cell_graph(rows)
        # 2 edges, not 4 — deduped by sorted-pair key
        assert counts["seed_valid"] == 2

    def test_first_placement_wins_per_cell(self) -> None:
        # Two blocks at same coord: Platform first, Deco second. Platform wins.
        rows = [
            (0, 0, 0, "Platform"),
            (0, 0, 0, "Deco"),       # duplicate cell, ignored
            (1, 0, 0, "Platform"),
        ]
        cells, _, counts = _build_cell_graph(rows)
        assert cells[(0, 0, 0)] == "Platform"
        assert counts["seed_valid"] == 1


class TestBuildAnchorSets:
    def test_multi_cell_gate_groups_by_tag_order(self) -> None:
        # 4 Goal cells at the same waypoint_order; 1 Spawn; 1 CP at
        # order=5 — three logical anchor sets.
        rows = [
            ("Goal", 0, 10, 0, 0),
            ("Goal", 0, 10, 1, 0),
            ("Goal", 0, 11, 0, 0),
            ("Goal", 0, 11, 1, 0),
            ("Spawn", 0, 0, 0, 0),
            ("LinkedCheckpoint", 5, 5, 0, 0),
        ]
        sets = _build_anchor_sets(rows)
        by_tag = {(s.tag, s.waypoint_order): s for s in sets}
        assert len(sets) == 3
        assert len(by_tag[("Goal", 0)].cells) == 4
        assert len(by_tag[("Spawn", 0)].cells) == 1
        assert len(by_tag[("LinkedCheckpoint", 5)].cells) == 1

    def test_different_orders_split_even_with_same_tag(self) -> None:
        rows = [
            ("LinkedCheckpoint", 1, 0, 0, 0),
            ("LinkedCheckpoint", 2, 1, 0, 0),
        ]
        sets = _build_anchor_sets(rows)
        assert len(sets) == 2

    def test_rows_with_null_coords_skipped(self) -> None:
        rows = [
            ("Goal", 0, None, 0, 0),     # null x
            ("Goal", 0, 0, None, 0),     # null y
            ("Goal", 0, 0, 0, None),     # null z
            ("Goal", 0, 1, 1, 1),        # valid
        ]
        sets = _build_anchor_sets(rows)
        assert len(sets) == 1
        assert len(sets[0].cells) == 1


class TestBFSReachable:
    def test_single_source_no_neighbors(self) -> None:
        visited = _bfs_reachable({}, frozenset({(0, 0, 0)}))
        assert visited == {(0, 0, 0)}

    def test_chain_traversed(self) -> None:
        nb = {
            (0, 0, 0): [(1, 0, 0)],
            (1, 0, 0): [(0, 0, 0), (2, 0, 0)],
            (2, 0, 0): [(1, 0, 0)],
        }
        assert _bfs_reachable(nb, frozenset({(0, 0, 0)})) == {
            (0, 0, 0), (1, 0, 0), (2, 0, 0),
        }

    def test_disconnected_components_not_reached(self) -> None:
        nb = {
            (0, 0, 0): [(1, 0, 0)],
            (1, 0, 0): [(0, 0, 0)],
            # Isolated island
            (10, 0, 0): [(11, 0, 0)],
            (11, 0, 0): [(10, 0, 0)],
        }
        visited = _bfs_reachable(nb, frozenset({(0, 0, 0)}))
        assert (10, 0, 0) not in visited
        assert (11, 0, 0) not in visited

    def test_multi_source(self) -> None:
        nb = {
            (0, 0, 0): [(1, 0, 0)],
            (1, 0, 0): [(0, 0, 0)],
            (10, 0, 0): [(11, 0, 0)],
            (11, 0, 0): [(10, 0, 0)],
        }
        visited = _bfs_reachable(nb, frozenset({(0, 0, 0), (10, 0, 0)}))
        assert visited == {(0, 0, 0), (1, 0, 0), (10, 0, 0), (11, 0, 0)}


class TestMapReachabilityFractions:
    def test_zero_edges_returns_zero(self) -> None:
        m = MapReachability(map_id=1)
        assert m.suppression_fraction == 0.0
        assert m.unsupported_fraction == 0.0
        assert m.reachability_fraction == 1.0  # no intervals → trivially all reached

    def test_all_unsupported(self) -> None:
        m = MapReachability(
            map_id=1, total_edges=10, unsupported_edges=10
        )
        assert m.unsupported_fraction == 1.0

    def test_reachability_partial(self) -> None:
        m = MapReachability(
            map_id=1, anchor_sets_total=4, anchor_sets_reachable=3
        )
        assert m.reachability_fraction == 0.75
        assert not m.passes_reachability

    def test_reachability_full_pass(self) -> None:
        m = MapReachability(
            map_id=1, anchor_sets_total=4, anchor_sets_reachable=4
        )
        assert m.passes_reachability


class TestUnionFind:
    def test_find_of_fresh_element_returns_itself(self) -> None:
        uf = _UnionFind()
        assert uf.find("a") == "a"

    def test_union_merges_two_elements(self) -> None:
        uf = _UnionFind()
        uf.union("a", "b")
        assert uf.same("a", "b")

    def test_union_is_transitive(self) -> None:
        uf = _UnionFind()
        uf.union("a", "b")
        uf.union("b", "c")
        assert uf.same("a", "c")

    def test_union_across_hashable_tuples(self) -> None:
        uf = _UnionFind()
        uf.union((0, 0, 0), (1, 0, 0))
        uf.union((1, 0, 0), (2, 0, 0))
        assert uf.same((0, 0, 0), (2, 0, 0))

    def test_disjoint_components_stay_disjoint(self) -> None:
        uf = _UnionFind()
        uf.union("a", "b")
        uf.union("c", "d")
        assert not uf.same("a", "c")


class TestBuildObservations:
    def _bc_file(self, tmp_path, cp_count: int) -> str:
        # Write a minimal breadcrumbs sidecar with the given CP count.
        path = tmp_path / f"r{cp_count}.breadcrumbs.json"
        path.write_text(
            f'{{"checkpoint_times_ms": {list(range(cp_count))}}}'
        )
        return str(path)

    def test_spawn_goal_only_when_no_cp_match(self, tmp_path) -> None:
        anchors = [
            AnchorSet(tag="Spawn", waypoint_order=0, cells=frozenset({(0, 0, 0)})),
            AnchorSet(tag="Goal", waypoint_order=0, cells=frozenset({(10, 0, 0)})),
            AnchorSet(tag="LinkedCheckpoint", waypoint_order=5,
                      cells=frozenset({(3, 0, 0)})),
            AnchorSet(tag="LinkedCheckpoint", waypoint_order=10,
                      cells=frozenset({(6, 0, 0)})),
        ]
        # Replay has 99 CP crossings — doesn't match 2 linked anchors
        replays = [(1, self._bc_file(tmp_path, 99))]
        obs = _build_observations(anchors, replays)
        assert len(obs) == 1
        assert obs[0].kind == "spawn_goal_only"
        assert obs[0].cells == frozenset({(0, 0, 0), (10, 0, 0)})

    def test_linked_ordered_match_includes_all_cps(self, tmp_path) -> None:
        anchors = [
            AnchorSet(tag="Spawn", waypoint_order=0, cells=frozenset({(0, 0, 0)})),
            AnchorSet(tag="Goal", waypoint_order=0, cells=frozenset({(10, 0, 0)})),
            AnchorSet(tag="LinkedCheckpoint", waypoint_order=5,
                      cells=frozenset({(3, 0, 0)})),
            AnchorSet(tag="LinkedCheckpoint", waypoint_order=10,
                      cells=frozenset({(6, 0, 0)})),
        ]
        # 2 linked + 1 finish = 3 timestamps. Or 2 linked alone. Accept both.
        replays = [(1, self._bc_file(tmp_path, 3))]
        obs = _build_observations(anchors, replays)
        assert len(obs) == 1
        assert obs[0].kind == "linked_ordered"
        assert (3, 0, 0) in obs[0].cells
        assert (6, 0, 0) in obs[0].cells

    def test_missing_sidecar_file_falls_back_to_spawn_goal(self, tmp_path) -> None:
        anchors = [
            AnchorSet(tag="Spawn", waypoint_order=0, cells=frozenset({(0, 0, 0)})),
            AnchorSet(tag="Goal", waypoint_order=0, cells=frozenset({(10, 0, 0)})),
        ]
        replays = [(1, str(tmp_path / "nonexistent.breadcrumbs.json"))]
        obs = _build_observations(anchors, replays)
        # Missing CP count → fall back to spawn+goal assertion
        assert len(obs) == 1
        assert obs[0].kind == "spawn_goal_only"

    def test_observation_dropped_when_fewer_than_two_anchors(self, tmp_path) -> None:
        anchors = [
            AnchorSet(tag="Spawn", waypoint_order=0, cells=frozenset({(0, 0, 0)})),
            # No goal
        ]
        replays = [(1, self._bc_file(tmp_path, 1))]
        obs = _build_observations(anchors, replays)
        # Only spawn cell asserted — can't be connected to anything else
        assert len(obs) == 0

    def test_no_replays_produces_no_observations(self, tmp_path) -> None:
        anchors = [
            AnchorSet(tag="Spawn", waypoint_order=0, cells=frozenset({(0, 0, 0)})),
            AnchorSet(tag="Goal", waypoint_order=0, cells=frozenset({(10, 0, 0)})),
        ]
        obs = _build_observations(anchors, [])
        assert obs == []


class TestMapReachabilityObservationFields:
    def test_observations_fields_default_to_zero(self) -> None:
        m = MapReachability(map_id=1)
        assert m.observations_available == 0
        assert m.observations_applied == 0
        assert m.anchor_sets_reachable_seed_only == 0


class TestValidationReportAggregation:
    def test_empty_report(self) -> None:
        r = ValidationReport()
        assert r.maps_total == 0
        assert r.interval_reachability_fraction == 0.0

    def test_weighted_fractions_are_edge_weighted(self) -> None:
        r = ValidationReport(per_map=[
            MapReachability(map_id=1, total_edges=100, unsupported_edges=80, unknown_edges=10),
            MapReachability(map_id=2, total_edges=50, unsupported_edges=20, unknown_edges=5),
        ])
        # Total edges = 150; total unsupported = 100 → 0.667
        assert abs(r.weighted_unsupported_fraction - 100 / 150) < 1e-6
        # Suppression = (80+10+20+5) / 150 = 115/150
        assert abs(r.weighted_suppression_fraction - 115 / 150) < 1e-6

    def test_maps_passing_count(self) -> None:
        r = ValidationReport(per_map=[
            MapReachability(map_id=1, anchor_sets_total=3, anchor_sets_reachable=3),
            MapReachability(map_id=2, anchor_sets_total=2, anchor_sets_reachable=1),
            MapReachability(map_id=3, anchor_sets_total=0),  # trivially passes
        ])
        assert r.maps_passing_reachability == 2
