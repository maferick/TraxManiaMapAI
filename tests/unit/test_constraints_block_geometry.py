"""Phase 2 #218-3 — tests for the block-geometry classifier.

Pure-function rules, no DB needed. Goal is to pin the classifier's
behaviour on the canonical block names we've seen in the corpus so
classifier_version bumps have a clear before/after.
"""
from __future__ import annotations

import pytest

from src.constraints.block_geometry import (
    CLASSIFIER_VERSION,
    SHAPE_CHECKPOINT,
    SHAPE_CURVE,
    SHAPE_DECO,
    SHAPE_FINISH,
    SHAPE_GATE,
    SHAPE_LOOP,
    SHAPE_PLATFORM,
    SHAPE_RAMP,
    SHAPE_START,
    SHAPE_STRAIGHT,
    SHAPE_SUPPORT,
    SHAPE_UNKNOWN,
    classify_block,
)


class TestShapeClassification:
    @pytest.mark.parametrize("family,name,expected", [
        # Race-role anchors — win over shape words
        ("Platform", "PlatformPlasticStart", SHAPE_START),
        ("Platform", "PlatformPlasticStartLoop1", SHAPE_START),
        ("Gate",     "GateCheckpoint", SHAPE_CHECKPOINT),
        ("Platform", "PlatformPlasticCheckpoint", SHAPE_CHECKPOINT),
        ("Platform", "PlatformPlasticCheckpointSlope2Up", SHAPE_CHECKPOINT),
        ("Platform", "PlatformPlasticFinish", SHAPE_FINISH),
        ("Gate",     "GateExpandableFinish", SHAPE_FINISH),
        ("Road",     "RoadTechMultilap", SHAPE_CHECKPOINT),
        # Shape words
        ("Platform", "PlatformPlasticLoopOutStartCurve1", SHAPE_START),  # anchor wins
        ("Platform", "PlatformPlasticLoop1", SHAPE_LOOP),
        ("Road",     "RoadDirtSlopeDiag1", SHAPE_RAMP),
        ("Road",     "RoadTechCurve1", SHAPE_CURVE),
        ("Road",     "RoadTechBend", SHAPE_CURVE),
        ("Road",     "RoadTechStraight", SHAPE_STRAIGHT),
        ("Road",     "RoadBump1", SHAPE_RAMP),
        # Support / structural
        ("Structure", "StructurePillar", SHAPE_SUPPORT),
        ("Structure", "StructureBase", SHAPE_SUPPORT),
        ("Structure", "StructureDeadend", SHAPE_SUPPORT),
        # Generic platform fallback
        ("Platform", "PlatformTechBase", SHAPE_SUPPORT),  # base wins
        ("Platform", "PlatformPlasticGenericPlatform", SHAPE_PLATFORM),
        # Gates that aren't anchors
        ("Gate", "GateSpecialSlowMotion", SHAPE_GATE),
        ("Gate", "GateGameplayStadium", SHAPE_GATE),
        # Unknown custom blocks stay unknown
        ("Unknown", "SomeCustomWackyThing", SHAPE_UNKNOWN),
    ])
    def test_shape_inference(
        self, family: str, name: str, expected: str,
    ) -> None:
        g = classify_block(family, name)
        assert g.shape_class == expected, f"{family}/{name}"

    def test_deco_family_defaults_to_deco_when_unknown_shape(self) -> None:
        g = classify_block("Deco", "DecoScrapFX1")
        assert g.shape_class == SHAPE_DECO
        assert g.is_deco is True

    def test_deco_family_keeps_explicit_shape(self) -> None:
        # A Deco-family block with "Curve" in the name stays classed
        # as a curve — don't silently demote explicit geometry words.
        g = classify_block("Deco", "DecoArchCurve1")
        assert g.shape_class == SHAPE_CURVE
        assert g.is_deco is True


class TestSurfaceInference:
    def test_family_map_wins(self) -> None:
        g = classify_block("RoadDirt", "RoadDirtStraight")
        assert g.surface_hint == "dirt"

    def test_falls_back_to_name_when_family_unknown(self) -> None:
        g = classify_block("Unknown", "SomeDirtRamp1")
        assert g.surface_hint == "dirt"

    def test_empty_when_no_hint(self) -> None:
        g = classify_block("Gate", "GateCheckpoint")
        assert g.surface_hint == ""


class TestAnchorCapable:
    @pytest.mark.parametrize("family,name", [
        ("Platform", "PlatformPlasticStart"),
        ("Gate", "GateCheckpoint"),
        ("Platform", "PlatformPlasticFinish"),
    ])
    def test_is_anchor_capable(self, family: str, name: str) -> None:
        assert classify_block(family, name).is_anchor_capable is True

    @pytest.mark.parametrize("family,name", [
        ("Road", "RoadTechStraight"),
        ("Structure", "StructurePillar"),
        ("Platform", "PlatformTechBase"),
    ])
    def test_not_anchor_capable(self, family: str, name: str) -> None:
        assert classify_block(family, name).is_anchor_capable is False


class TestVersionPinning:
    def test_classifier_version_is_stamped(self) -> None:
        g = classify_block("Road", "RoadTechStraight")
        assert g.classifier_version == CLASSIFIER_VERSION
