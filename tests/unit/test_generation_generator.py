"""Phase 2 PR E — tests for the minimal generator.

Exercises the parts that don't need a real DB:
- ``GenerationInputs`` validation
- ``_compute_run_id`` determinism
- Artifact builder produces a schema-valid dict on both happy path
  and AssemblyError paths
- Schema validation catches drift from spec

The DB-touching ``generate_from_base`` is covered by a live smoke
in the PR body; unit tests here use the lower-level ``_build_artifact``
with hand-built ``_BaseMapData`` / route results.
"""
from __future__ import annotations

import json

import pytest

from src.generation import (
    Anchor,
    AssembledRoute,
    AssemblyError,
    ChosenCorridor,
    FinishabilityResult,
    GenerationInputs,
    IntervalAssembly,
    validate_generated_map,
)
from src.generation.generator import (
    _BaseMapData,
    _build_artifact,
    _compute_run_id,
)


# ---------------------------------------------------------------------
# GenerationInputs
# ---------------------------------------------------------------------

class TestGenerationInputs:
    def test_accepts_valid_inputs(self) -> None:
        i = GenerationInputs(
            base_map_id=1, base_map_source_id="42",
            style_tag_filter="Tech", difficulty="medium", random_seed=7,
        )
        assert i.style_tag_filter == "Tech"
        assert i.difficulty == "medium"

    def test_rejects_bad_style(self) -> None:
        with pytest.raises(ValueError, match="style_tag_filter"):
            GenerationInputs(
                base_map_id=1, base_map_source_id=None,
                style_tag_filter="SpeedDrift",
            )

    def test_rejects_bad_difficulty(self) -> None:
        with pytest.raises(ValueError, match="difficulty"):
            GenerationInputs(
                base_map_id=1, base_map_source_id=None,
                difficulty="legendary",
            )

    def test_style_none_is_valid(self) -> None:
        i = GenerationInputs(base_map_id=1, base_map_source_id=None)
        assert i.style_tag_filter is None


# ---------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------

class TestRunIdDeterminism:
    def test_same_inputs_same_run_id(self) -> None:
        a = GenerationInputs(
            base_map_id=1, base_map_source_id="x",
            style_tag_filter="Tech", difficulty="hard", random_seed=42,
        )
        b = GenerationInputs(
            base_map_id=1, base_map_source_id="x",
            style_tag_filter="Tech", difficulty="hard", random_seed=42,
        )
        assert _compute_run_id(a) == _compute_run_id(b)

    def test_different_seed_different_id(self) -> None:
        a = GenerationInputs(base_map_id=1, base_map_source_id="x",
                             random_seed=1)
        b = GenerationInputs(base_map_id=1, base_map_source_id="x",
                             random_seed=2)
        assert _compute_run_id(a) != _compute_run_id(b)

    def test_run_id_is_16_hex(self) -> None:
        rid = _compute_run_id(
            GenerationInputs(base_map_id=1, base_map_source_id="x"),
        )
        assert len(rid) == 16
        int(rid, 16)


# ---------------------------------------------------------------------
# Artifact construction + schema validation
# ---------------------------------------------------------------------

def _happy_base() -> _BaseMapData:
    return _BaseMapData(
        source_map_id="12345",
        blocks=[
            {"block_family": "RoadTech", "block_name": "RoadTechStraight",
             "x": 0, "y": 0, "z": 0, "rotation": 0},
            {"block_family": "RoadTech", "block_name": "RoadTechStraight",
             "x": 0, "y": 0, "z": 1, "rotation": 0},
        ],
        checkpoints=[
            {"waypoint_index": 0, "waypoint_order": 0, "tag": "Spawn",
             "x": 0, "y": 0, "z": 0},
            {"waypoint_index": 1, "waypoint_order": 1, "tag": "Checkpoint",
             "x": 0, "y": 0, "z": 5},
            {"waypoint_index": 2, "waypoint_order": 0, "tag": "Goal",
             "x": 0, "y": 0, "z": 10},
        ],
        model_hash="a" * 64,
        learned_score_version="time_envelope_v2_weighted@0.1.0",
    )


def _happy_route() -> AssembledRoute:
    spawn = Anchor("Spawn", 0, (0, 0, 0))
    cp1 = Anchor("Checkpoint", 1, (0, 0, 5))
    goal = Anchor("Goal", 0, (0, 0, 10))
    c1 = ChosenCorridor(
        corridor_id=100, map_id=1,
        src=spawn, dst=cp1,
        path_cells=((0, 0, 0), (0, 0, 5)),
        path_length=6, contains_virtual_edge=False,
        corridor_confidence=0.8,
        learned_corridor_score=0.75,
        expected_time_ms=6400,
    )
    c2 = ChosenCorridor(
        corridor_id=101, map_id=1,
        src=cp1, dst=goal,
        path_cells=((0, 0, 5), (0, 0, 10)),
        path_length=6, contains_virtual_edge=False,
        corridor_confidence=0.85,
        learned_corridor_score=0.70,
        expected_time_ms=6400,
    )
    return AssembledRoute(
        map_id=1,
        anchors=(spawn, cp1, goal),
        intervals=(
            IntervalAssembly(index=0, src=spawn, dst=cp1, chosen=c1),
            IntervalAssembly(index=1, src=cp1, dst=goal, chosen=c2),
        ),
        cells_total=12,
        estimated_time_ms=12800,
        ai_confidence=0.725,
    )


def _happy_gate(route: AssembledRoute) -> FinishabilityResult:
    return FinishabilityResult(
        route_verified=True,
        estimated_time_ms=route.estimated_time_ms,
        ai_confidence=route.ai_confidence,
        reject_reason=None,
        gate_version="finishability-v0",
    )


class TestHappyPathArtifact:
    def _build(self):
        inputs = GenerationInputs(
            base_map_id=1, base_map_source_id="12345",
            style_tag_filter="Tech", difficulty="medium", random_seed=42,
        )
        base = _happy_base()
        route = _happy_route()
        gate = _happy_gate(route)
        return _build_artifact(
            inputs=inputs, base=base, route=route, gate=gate,
            config_hash="cfg", sha="sha",
        )

    def test_artifact_passes_schema(self) -> None:
        art = self._build()
        assert validate_generated_map(art) is None

    def test_route_verified_surfaced(self) -> None:
        art = self._build()
        assert art["finishability"]["route_verified"] is True
        assert art["finishability"]["reject_reason"] is None
        assert art["finishability"]["ai_confidence"] == pytest.approx(0.725)

    def test_intervals_and_corridors_used_present(self) -> None:
        art = self._build()
        assert len(art["route"]["intervals"]) == 2
        assert len(art["route"]["corridors_used"]) == 2
        assert art["route"]["cells_total"] == 12

    def test_provenance_complete(self) -> None:
        art = self._build()
        p = art["provenance"]
        assert p["model_hash"] == "a" * 64
        assert p["learned_score_version"] == "time_envelope_v2_weighted@0.1.0"
        assert p["config_hash"] == "cfg"
        assert p["code_version"] == "sha"
        assert p["classification_version"]  # non-empty

    def test_run_id_reproducible_across_builds(self) -> None:
        art1 = self._build()
        art2 = self._build()
        # generated_at may differ but run_id is inputs-only.
        assert art1["run_id"] == art2["run_id"]


# ---------------------------------------------------------------------
# Reject-path artifact
# ---------------------------------------------------------------------

class TestRejectArtifact:
    def test_plain_cp_reject_validates(self) -> None:
        inputs = GenerationInputs(
            base_map_id=1, base_map_source_id="12345",
            style_tag_filter=None, difficulty="medium", random_seed=42,
        )
        base = _happy_base()
        err = AssemblyError(
            reason="plain_cp_not_supported_v0",
            detail="demo plain-CP rejection",
        )
        gate = FinishabilityResult(
            route_verified=False,
            estimated_time_ms=None,
            ai_confidence=None,
            reject_reason="plain_cp_not_supported_v0",
            gate_version="finishability-v0",
            detail="demo plain-CP rejection",
        )
        art = _build_artifact(
            inputs=inputs, base=base, route=err, gate=gate,
            config_hash="cfg", sha="sha",
        )
        # Schema should accept the reject shape.
        assert validate_generated_map(art) is None
        # Route block must be present but empty.
        assert art["route"]["intervals"] == []
        assert art["route"]["corridors_used"] == []
        # Fin block propagates reject_reason + detail.
        assert art["finishability"]["route_verified"] is False
        assert art["finishability"]["reject_reason"] == "plain_cp_not_supported_v0"
        assert art["finishability"]["detail"] == "demo plain-CP rejection"

    def test_confidence_floor_reject_surfaces_numbers(self) -> None:
        # Gate rejected on confidence_below_floor but numbers are kept
        # so operator can see the diagnostic. Artifact must still
        # schema-validate.
        inputs = GenerationInputs(
            base_map_id=1, base_map_source_id="12345",
        )
        base = _happy_base()
        route = _happy_route()
        gate = FinishabilityResult(
            route_verified=False,
            estimated_time_ms=10_000,
            ai_confidence=0.20,
            reject_reason="confidence_below_floor",
            gate_version="finishability-v0",
            detail="ai_confidence 0.200 below floor 0.30",
        )
        art = _build_artifact(
            inputs=inputs, base=base, route=route, gate=gate,
            config_hash="cfg", sha="sha",
        )
        assert validate_generated_map(art) is None
        assert art["finishability"]["estimated_time_ms"] == 10_000
        assert art["finishability"]["ai_confidence"] == pytest.approx(0.20)

    def test_serializable_to_json(self) -> None:
        art = TestHappyPathArtifact()._build()
        # Must round-trip through json.dumps without failure
        # (floats / ints only, no datetime objects leaking through).
        s = json.dumps(art)
        art2 = json.loads(s)
        assert art == art2
