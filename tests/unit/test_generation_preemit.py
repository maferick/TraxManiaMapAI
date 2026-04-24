"""Tests for :mod:`src.generation.preemit`.

Focus on the orchestration layer: block-shape normalisation, summary
aggregation, top_findings ordering, JSON round-trip. The underlying
validators are covered by their own tests; here we just check the
wrapper doesn't lose or scramble data.
"""
from __future__ import annotations

import json

import pytest

from src.generation.geom_validator import (
    CODE_PARTIAL_MULTICELL,
    CODE_ROUTE_CELL_MISSING_BLOCK,
    SEVERITY_FAIL,
    SEVERITY_WARN,
    GeometryInfo,
)
from src.generation.preemit import (
    PREEMIT_VERSION,
    run_preemit_validation,
)


_WALL4 = GeometryInfo(
    footprint_x=4, shape_class="straight", connector_hint="straight_x",
)
_STRAIGHT = GeometryInfo(shape_class="straight")
_START = GeometryInfo(shape_class="start", is_anchor_capable=True)


class TestNormalisation:
    """DB rows (``block_family``) and wrapper dicts (``family``) both
    accepted — don't make callers remember which shape they have."""

    def test_db_shape_accepted(self) -> None:
        blocks = [{
            "block_family": "Platform",
            "block_name": "PlatformPlasticWallStraight4",
            "x": 5, "y": 10, "z": 7, "rotation": 0,
        }]
        lookup = {("Platform", "PlatformPlasticWallStraight4"): _WALL4}
        summary = run_preemit_validation(
            blocks=blocks, geometry_lookup=lookup,
        )
        # Three missing shadow cells → three fails.
        assert summary.fail_count == 3
        assert summary.code_counts[CODE_PARTIAL_MULTICELL] == 3

    def test_wrapper_shape_accepted(self) -> None:
        blocks = [{
            "placement": "grid",
            "family": "Platform",
            "name": "PlatformPlasticWallStraight4",
            "x": 5, "y": 10, "z": 7, "rotation": 0,
        }]
        lookup = {("Platform", "PlatformPlasticWallStraight4"): _WALL4}
        summary = run_preemit_validation(
            blocks=blocks, geometry_lookup=lookup,
        )
        assert summary.fail_count == 3


class TestSummaryAggregation:
    def test_empty_map_empty_summary(self) -> None:
        summary = run_preemit_validation(blocks=[], geometry_lookup={})
        assert summary.fail_count == 0
        assert summary.warn_count == 0
        assert summary.info_count == 0
        assert summary.jump_class_counts == {}   # no route → no jump pass
        assert summary.top_findings == ()

    def test_version_stamped(self) -> None:
        summary = run_preemit_validation(blocks=[], geometry_lookup={})
        assert summary.version == PREEMIT_VERSION

    def test_route_triggers_jump_pass(self) -> None:
        # Simple route: straight → straight — no jumps, no gaps. Should
        # still populate jump_class_counts with zeros (signals the
        # pass ran).
        blocks = [
            {"block_family": "Road", "block_name": "RoadTechStraight",
             "x": 0, "y": 9, "z": 0, "rotation": 0},
            {"block_family": "Road", "block_name": "RoadTechStraight",
             "x": 1, "y": 9, "z": 0, "rotation": 0},
        ]
        lookup = {("Road", "RoadTechStraight"): _STRAIGHT}
        summary = run_preemit_validation(
            blocks=blocks, geometry_lookup=lookup,
            route_cells=[(0, 9, 0), (1, 9, 0)],
        )
        # jump pass runs; nothing qualifies as a candidate.
        assert summary.jump_class_counts != {}
        assert all(v == 0 for v in summary.jump_class_counts.values())

    def test_combined_pipeline(self) -> None:
        # Broken multi-cell + broken jump + missing route block, all
        # in one shot. Numbers match what the individual validators
        # would have produced alone.
        blocks = [
            # Wall4 with empty shadow cells → 3 FAIL partial_multicell
            {"block_family": "Platform",
             "block_name": "PlatformPlasticWallStraight4",
             "x": 5, "y": 10, "z": 7, "rotation": 0},
            # Spawn anchor at (0,10,0), straight at (1,10,0), gap to
            # (5,10,0) with nothing — route_gap (WARN) +
            # route_cell_missing_block (FAIL) + likely_broken jump
            # (FAIL) — ramp shape is on the straight so it doesn't
            # take off, and the gap is only cheb=4 with no landing.
            {"block_family": "Platform",
             "block_name": "PlatformPlasticStart",
             "x": 0, "y": 10, "z": 0, "rotation": 0},
            {"block_family": "Road", "block_name": "RoadTechRamp",
             "x": 1, "y": 10, "z": 0, "rotation": 0},
        ]
        lookup = {
            ("Platform", "PlatformPlasticWallStraight4"): _WALL4,
            ("Platform", "PlatformPlasticStart"): _START,
            ("Road", "RoadTechRamp"): GeometryInfo(shape_class="ramp"),
        }
        summary = run_preemit_validation(
            blocks=blocks, geometry_lookup=lookup,
            route_cells=[(0, 10, 0), (1, 10, 0), (5, 10, 0)],
            spawn_cell=(0, 10, 0),
        )
        # 3 multicell fails + 1 missing-block fail + 1 likely-broken
        # jump fail = 5. Allow ≥5 in case a future check adds more.
        assert summary.fail_count >= 5
        assert summary.code_counts.get(CODE_PARTIAL_MULTICELL, 0) == 3
        assert summary.code_counts.get(
            CODE_ROUTE_CELL_MISSING_BLOCK, 0,
        ) == 1


class TestTopFindingsOrdering:
    def test_fail_ordered_before_warn(self) -> None:
        # Multi-cell block (fails) + a route gap with no block at
        # end cell (fails) + a large-gap warn.
        blocks = [
            {"block_family": "Platform",
             "block_name": "PlatformPlasticWallStraight4",
             "x": 0, "y": 10, "z": 0, "rotation": 0},
        ]
        lookup = {
            ("Platform", "PlatformPlasticWallStraight4"): _WALL4,
            ("Road", "RoadTechStraight"): _STRAIGHT,
        }
        # Far-apart route cells trigger route_gap WARN + missing-block FAILs.
        summary = run_preemit_validation(
            blocks=blocks, geometry_lookup=lookup,
            route_cells=[(100, 50, 100), (110, 50, 110)],
            max_route_step_cheb=1,
        )
        severities = [f.severity for f in summary.top_findings]
        # FAIL bucket must precede WARN bucket in the head-of-list.
        first_warn = next(
            (i for i, s in enumerate(severities) if s == SEVERITY_WARN),
            None,
        )
        if first_warn is not None:
            assert all(
                s == SEVERITY_FAIL for s in severities[:first_warn]
            )


class TestPerCorridorScores:
    """Per-corridor validation score — the soft generation signal.

    Formula is conservative by design: a clean corridor scores 1.0;
    a corridor with one likely_broken jump + a partial_multicell hit
    sits around ~0.6; a catastrophic corridor floors at 0.0.
    """

    def test_clean_corridor_scores_1_0(self) -> None:
        blocks = [
            {"block_family": "Road", "block_name": "RoadTechStraight",
             "x": 0, "y": 9, "z": 0, "rotation": 0},
            {"block_family": "Road", "block_name": "RoadTechStraight",
             "x": 1, "y": 9, "z": 0, "rotation": 0},
        ]
        lookup = {("Road", "RoadTechStraight"): _STRAIGHT}
        summary = run_preemit_validation(
            blocks=blocks, geometry_lookup=lookup,
            route_cells=[(0, 9, 0), (1, 9, 0)],
            corridor_paths=[(99, 0, [(0, 9, 0), (1, 9, 0)])],
        )
        assert len(summary.per_corridor_scores) == 1
        cs = summary.per_corridor_scores[0]
        assert cs.corridor_id == 99
        assert cs.validation_score == pytest.approx(1.0)
        assert cs.partial_multicell_hits == 0
        assert cs.jump_likely_broken == 0

    def test_broken_corridor_loses_score(self) -> None:
        # Ramp takeoff, no landing in cone, plus a nearby empty-
        # shadow multi-cell block. Score should drop meaningfully.
        wall_lookup = GeometryInfo(
            footprint_x=4, shape_class="straight",
        )
        ramp_lookup = GeometryInfo(shape_class="ramp")
        blocks = [
            {"block_family": "Road", "block_name": "RoadTechRamp",
             "x": 0, "y": 10, "z": 0, "rotation": 0},
            # Wall4 whose 3 shadow cells are empty, near the path.
            {"block_family": "Platform",
             "block_name": "PlatformPlasticWallStraight4",
             "x": 2, "y": 10, "z": 0, "rotation": 0},
        ]
        lookup = {
            ("Road", "RoadTechRamp"): ramp_lookup,
            ("Platform", "PlatformPlasticWallStraight4"): wall_lookup,
        }
        summary = run_preemit_validation(
            blocks=blocks, geometry_lookup=lookup,
            route_cells=[(0, 10, 0), (10, 10, 0)],
            corridor_paths=[(7, 0, [(0, 10, 0), (10, 10, 0)])],
        )
        cs = summary.per_corridor_scores[0]
        # The ramp launches toward (10,10,0); the Wall4 mesh at
        # (2..5,10,0) lands inside the cone but isn't aligned with
        # the next route cell — broken OR uncertain is correct.
        assert cs.jump_likely_broken + cs.jump_uncertain >= 1
        # Partial-multicell fails nearby (the Wall4's 3 empty shadows
        # aren't actually empty here because the Wall4 origin *is* a
        # block; instead its own shadow is empty) should still push
        # the score visibly below 1.0.
        assert cs.validation_score < 0.9

    def test_score_floored_at_zero(self) -> None:
        # 50 ramp → nothing jumps; enough penalty to drive below 0,
        # should clamp to 0.0.
        blocks = [
            {"block_family": "Road", "block_name": "RoadTechRamp",
             "x": i * 10, "y": 10, "z": 0, "rotation": 0}
            for i in range(50)
        ]
        lookup = {("Road", "RoadTechRamp"): GeometryInfo(shape_class="ramp")}
        route = [(i * 10, 10, 0) for i in range(50)]
        summary = run_preemit_validation(
            blocks=blocks, geometry_lookup=lookup,
            route_cells=route,
            corridor_paths=[(1, 0, route)],
        )
        cs = summary.per_corridor_scores[0]
        assert cs.validation_score == 0.0

    def test_no_corridor_paths_means_no_per_corridor_scores(self) -> None:
        # Backwards-compat: callers that didn't opt into corridor
        # scoring get an empty tuple (not None).
        summary = run_preemit_validation(
            blocks=[{
                "block_family": "Road", "block_name": "RoadTechStraight",
                "x": 0, "y": 9, "z": 0, "rotation": 0,
            }],
            geometry_lookup={("Road", "RoadTechStraight"): _STRAIGHT},
        )
        assert summary.per_corridor_scores == ()


class TestToDict:
    """JSON sidecar round-trip: the shape consumers read back must
    be stable across runs."""

    def test_to_dict_is_json_serialisable(self) -> None:
        blocks = [{
            "block_family": "Platform",
            "block_name": "PlatformPlasticWallStraight4",
            "x": 5, "y": 10, "z": 7, "rotation": 0,
        }]
        lookup = {("Platform", "PlatformPlasticWallStraight4"): _WALL4}
        summary = run_preemit_validation(
            blocks=blocks, geometry_lookup=lookup,
        )
        payload = summary.to_dict()
        # Round-trip through json to catch tuple/Cell/Enum leakage.
        reloaded = json.loads(json.dumps(payload))
        assert reloaded["version"] == PREEMIT_VERSION
        assert reloaded["fail_count"] == 3
        assert reloaded["code_counts"][CODE_PARTIAL_MULTICELL] == 3
        assert reloaded["top_findings"][0]["severity"] == SEVERITY_FAIL
        # cells round-trip as lists (JSON has no tuples).
        assert reloaded["top_findings"][0]["cell"] == [6, 10, 7]
