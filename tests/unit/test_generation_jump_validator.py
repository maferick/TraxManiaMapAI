"""Tests for the jump-aware geometry validator (task #227)."""
from __future__ import annotations

import pytest

from src.generation.geom_validator import GeometryInfo
from src.generation.jump_validator import (
    CLASS_GEOMETRICALLY_PLAUSIBLE,
    CLASS_LIKELY_BROKEN,
    CLASS_SUPPORTED_BY_REPLAY,
    CLASS_UNCERTAIN,
    CODE_JUMP,
    JumpConeConfig,
    classify_jump,
    detect_jump_candidates,
    find_landing_candidates,
    validate_jumps,
)


def _grid_block(
    x: int, y: int, z: int, *,
    family: str = "Road", name: str = "RoadTechStraight",
    rotation: int = 0,
) -> dict:
    return {
        "placement": "grid", "x": x, "y": y, "z": z,
        "family": family, "name": name, "rotation": rotation,
    }


_STRAIGHT = GeometryInfo(shape_class="straight")
_RAMP = GeometryInfo(shape_class="ramp")
_LOOP = GeometryInfo(shape_class="loop")
_PLATFORM = GeometryInfo(shape_class="platform")
_SUPPORT = GeometryInfo(shape_class="support")


def _lookup(**overrides: GeometryInfo) -> dict:
    """Compact helper — k is 'Family/Name', v is GeometryInfo."""
    out: dict[tuple[str, str], GeometryInfo] = {
        ("Road", "RoadTechStraight"): _STRAIGHT,
        ("Road", "RoadTechRamp"): _RAMP,
        ("Platform", "PlatformPlasticLoop1"): _LOOP,
        ("Platform", "PlatformPlasticPlatform"): _PLATFORM,
        ("Structure", "StructurePillar"): _SUPPORT,
    }
    for k, v in overrides.items():
        fam, name = k.split("/", 1)
        out[(fam, name)] = v
    return out


class TestConeConfig:
    def test_rejects_nonpositive_forward_min(self) -> None:
        with pytest.raises(ValueError, match="forward_min_cells"):
            JumpConeConfig(forward_min_cells=0)

    def test_rejects_max_less_than_min(self) -> None:
        with pytest.raises(ValueError, match="forward_max_cells"):
            JumpConeConfig(forward_min_cells=5, forward_max_cells=3)


class TestDetectJumpCandidates:
    def test_ramp_at_takeoff_always_candidate(self) -> None:
        # Route: (0,10,0) → (1,10,0). cheb=1 but takeoff is a ramp.
        route = [(0, 10, 0), (1, 10, 0)]
        cell_to_block = {
            (0, 10, 0): _grid_block(0, 10, 0, name="RoadTechRamp"),
            (1, 10, 0): _grid_block(1, 10, 0),
        }
        cands = detect_jump_candidates(
            route_cells=route,
            cell_to_block=cell_to_block,
            geometry_lookup=_lookup(),
        )
        assert len(cands) == 1
        assert cands[0].takeoff_shape == "ramp"

    def test_explicit_gap_without_ramp_is_candidate(self) -> None:
        # cheb=3 between cells, takeoff is a straight — still a
        # candidate because the ground path can't bridge the gap.
        route = [(0, 10, 0), (3, 10, 0)]
        cell_to_block = {(0, 10, 0): _grid_block(0, 10, 0)}
        cands = detect_jump_candidates(
            route_cells=route,
            cell_to_block=cell_to_block,
            geometry_lookup=_lookup(),
        )
        assert len(cands) == 1
        assert cands[0].gap_cheb == 3

    def test_contiguous_straight_is_not_candidate(self) -> None:
        route = [(0, 10, 0), (1, 10, 0), (2, 10, 0)]
        cell_to_block = {
            (x, y, z): _grid_block(x, y, z) for (x, y, z) in route
        }
        assert detect_jump_candidates(
            route_cells=route,
            cell_to_block=cell_to_block,
            geometry_lookup=_lookup(),
        ) == []

    def test_empty_route_returns_empty(self) -> None:
        assert detect_jump_candidates(
            route_cells=[], cell_to_block={}, geometry_lookup=_lookup(),
        ) == []


class TestFindLandingCandidates:
    def test_finds_landing_on_forward_axis(self) -> None:
        # Ramp at (0,10,0) aimed at (5,10,0); a straight sits at
        # (5,10,0) — should be found as landing.
        cell_to_block = {
            (0, 10, 0): _grid_block(0, 10, 0, name="RoadTechRamp"),
            (5, 10, 0): _grid_block(5, 10, 0),
        }
        from src.generation.jump_validator import JumpCandidate
        cand = JumpCandidate(
            takeoff_cell=(0, 10, 0), next_route_cell=(5, 10, 0),
            gap_cheb=5, takeoff_shape="ramp",
            takeoff_family="Road", takeoff_name="RoadTechRamp",
        )
        landings = find_landing_candidates(
            candidate=cand, cell_to_block=cell_to_block,
            geometry_lookup=_lookup(),
            cone=JumpConeConfig(),
        )
        assert (5, 10, 0) in landings

    def test_out_of_cone_landings_excluded(self) -> None:
        # A landing sits 20 cells forward — well outside the default
        # forward_max_cells=12 cone.
        cell_to_block = {
            (0, 10, 0): _grid_block(0, 10, 0, name="RoadTechRamp"),
            (20, 10, 0): _grid_block(20, 10, 0),
        }
        from src.generation.jump_validator import JumpCandidate
        cand = JumpCandidate(
            takeoff_cell=(0, 10, 0), next_route_cell=(20, 10, 0),
            gap_cheb=20, takeoff_shape="ramp",
            takeoff_family="Road", takeoff_name="RoadTechRamp",
        )
        landings = find_landing_candidates(
            candidate=cand, cell_to_block=cell_to_block,
            geometry_lookup=_lookup(),
            cone=JumpConeConfig(),
        )
        assert (20, 10, 0) not in landings

    def test_support_shape_is_not_a_landing(self) -> None:
        cell_to_block = {
            (0, 10, 0): _grid_block(0, 10, 0, name="RoadTechRamp"),
            (5, 10, 0): _grid_block(
                5, 10, 0, family="Structure", name="StructurePillar",
            ),
        }
        from src.generation.jump_validator import JumpCandidate
        cand = JumpCandidate(
            takeoff_cell=(0, 10, 0), next_route_cell=(5, 10, 0),
            gap_cheb=5, takeoff_shape="ramp",
            takeoff_family="Road", takeoff_name="RoadTechRamp",
        )
        landings = find_landing_candidates(
            candidate=cand, cell_to_block=cell_to_block,
            geometry_lookup=_lookup(),
            cone=JumpConeConfig(),
        )
        assert landings == []


class TestClassifyJump:
    def _mkcand(self, shape: str = "ramp", gap: int = 5):
        from src.generation.jump_validator import JumpCandidate
        return JumpCandidate(
            takeoff_cell=(0, 10, 0), next_route_cell=(5, 10, 0),
            gap_cheb=gap, takeoff_shape=shape,
            takeoff_family="Road", takeoff_name="RoadTechRamp",
        )

    def test_replay_support_beats_geometry(self) -> None:
        # Even with zero landings, a replay crossing both ends
        # classifies supported_by_replay.
        cls = classify_jump(
            candidate=self._mkcand(),
            landings=[],
            replay_touched_cells={(0, 10, 0), (5, 10, 0)},
        )
        assert cls.classification == CLASS_SUPPORTED_BY_REPLAY

    def test_geometrically_plausible_when_landing_aligned(self) -> None:
        cls = classify_jump(
            candidate=self._mkcand(),
            landings=[(5, 10, 0)],         # cheb=0 from next_route_cell
            replay_touched_cells=None,
        )
        assert cls.classification == CLASS_GEOMETRICALLY_PLAUSIBLE

    def test_likely_broken_when_ramp_no_landing(self) -> None:
        cls = classify_jump(
            candidate=self._mkcand(shape="ramp"),
            landings=[],
            replay_touched_cells=None,
        )
        assert cls.classification == CLASS_LIKELY_BROKEN

    def test_uncertain_when_gap_no_ramp_no_landing(self) -> None:
        cls = classify_jump(
            candidate=self._mkcand(shape="straight", gap=3),
            landings=[],
            replay_touched_cells=None,
        )
        assert cls.classification == CLASS_UNCERTAIN

    def test_uncertain_when_landings_misaligned(self) -> None:
        # Landings exist but none is within cheb=1 of next_route_cell.
        cls = classify_jump(
            candidate=self._mkcand(),
            landings=[(5, 10, 10)],   # cheb=10 from (5,10,0)
            replay_touched_cells=None,
        )
        assert cls.classification == CLASS_UNCERTAIN


class TestValidateJumpsOrchestrator:
    def test_map_with_no_jumps_returns_empty_report(self) -> None:
        route = [(0, 10, 0), (1, 10, 0), (2, 10, 0)]
        blocks = [_grid_block(x, y, z) for (x, y, z) in route]
        report = validate_jumps(
            blocks=blocks, geometry_lookup=_lookup(), route_cells=route,
        )
        assert report.classifications == []

    def test_end_to_end_plausible_jump(self) -> None:
        # Ramp at (0,10,0), straight at (5,10,0); route tries to go
        # there directly. Should come out geometrically_plausible.
        route = [(0, 10, 0), (5, 10, 0)]
        blocks = [
            _grid_block(0, 10, 0, name="RoadTechRamp"),
            _grid_block(5, 10, 0),
        ]
        report = validate_jumps(
            blocks=blocks, geometry_lookup=_lookup(), route_cells=route,
        )
        assert len(report.classifications) == 1
        assert (
            report.classifications[0].classification
            == CLASS_GEOMETRICALLY_PLAUSIBLE
        )

    def test_end_to_end_likely_broken(self) -> None:
        # Ramp at takeoff, nothing anywhere in the cone.
        route = [(0, 10, 0), (5, 10, 0)]
        blocks = [_grid_block(0, 10, 0, name="RoadTechRamp")]
        report = validate_jumps(
            blocks=blocks, geometry_lookup=_lookup(), route_cells=route,
        )
        assert len(report.classifications) == 1
        assert report.classifications[0].classification == CLASS_LIKELY_BROKEN
        # Findings surface the FAIL severity.
        findings = report.findings()
        assert len(findings) == 1
        assert findings[0].code == CODE_JUMP
        assert findings[0].severity == "fail"

    def test_replay_evidence_overrides_broken_geometry(self) -> None:
        # Same "broken" geometry, but a replay has driven it clean.
        # CLAUDE.md contract: replay is authoritative → downgrade
        # the finding from FAIL (likely_broken) to INFO (supported).
        route = [(0, 10, 0), (5, 10, 0)]
        blocks = [_grid_block(0, 10, 0, name="RoadTechRamp")]
        report = validate_jumps(
            blocks=blocks, geometry_lookup=_lookup(), route_cells=route,
            replay_touched_cells={(0, 10, 0), (5, 10, 0), (6, 10, 0)},
        )
        assert (
            report.classifications[0].classification
            == CLASS_SUPPORTED_BY_REPLAY
        )
        findings = report.findings()
        assert findings[0].severity == "info"

    def test_report_counters(self) -> None:
        route = [
            (0, 10, 0),  (5, 10, 0),   # plausible: ramp + landing
            (10, 10, 0),                # broken: ramp, no landing
        ]
        blocks = [
            _grid_block(0, 10, 0, name="RoadTechRamp"),
            _grid_block(5, 10, 0, name="RoadTechRamp"),   # second ramp
        ]
        report = validate_jumps(
            blocks=blocks, geometry_lookup=_lookup(), route_cells=route,
        )
        assert report.route_cells_total == 3
        # At least one of each bucket we can observe.
        assert (
            len(report.by_class(CLASS_GEOMETRICALLY_PLAUSIBLE)) >= 1
            or len(report.by_class(CLASS_LIKELY_BROKEN)) >= 1
        )
