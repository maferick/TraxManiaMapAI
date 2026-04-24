"""Phase 2 #218-3 — tests for the block-geometry classifier.

Pure-function rules, no DB needed. Goal is to pin the classifier's
behaviour on the canonical block names we've seen in the corpus so
classifier_version bumps have a clear before/after.
"""
from __future__ import annotations

import pytest

from src.constraints.block_geometry import (
    CLASSIFIER_VERSION,
    CONNECTOR_ANCHOR,
    CONNECTOR_CURVE_XZ,
    CONNECTOR_LOOP_Y,
    CONNECTOR_NONE,
    CONNECTOR_PLATFORM,
    CONNECTOR_SLOPE_XY,
    CONNECTOR_STRAIGHT_X,
    PLACEMENT_FREE_ONLY,
    PLACEMENT_GRID_ONLY,
    PLACEMENT_MIXED,
    PLACEMENT_UNKNOWN,
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
    _placement_mode_from_counts,
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

    def test_classifier_version_is_v1_1(self) -> None:
        # Load-bearing: #218-6 bumped this; generation code branches
        # on the version string so a regression here would silently
        # keep downstream tables stuck at v1.0 behaviour.
        assert CLASSIFIER_VERSION == "v1.1.0"


class TestFootprintInference:
    """#218-6 — name-pattern length inference.

    Curves and loops decline to guess (irregular); only linear shapes
    carry a usable suffix signal in the TM2020 naming convention.
    """

    @pytest.mark.parametrize("family,name,expected", [
        # Straight length suffix — the common multi-cell offenders.
        ("Platform", "PlatformPlasticWallStraight4", (4, 1, 1)),
        ("Platform", "PlatformPlasticStraight2",     (2, 1, 1)),
        ("Road",     "RoadTechStraight",             (1, 1, 1)),  # no suffix
        # Slope suffix — slopes are ramps with an X-length.
        ("Platform", "PlatformPlasticSlope2Straight", (2, 1, 1)),
        ("Road",     "RoadDirtSlope3",                (3, 1, 1)),
        # TiltTransition carries its length the same way.
        ("Platform", "PlatformPlasticTiltTransition2UpLeft", (2, 1, 1)),
        # Curves decline to guess — irregular footprint.
        ("Road",     "RoadTechCurve3",                (1, 1, 1)),
        # Loops decline to guess too.
        ("Platform", "PlatformPlasticLoop2",          (1, 1, 1)),
        # Unknown / deco decline.
        ("Unknown",  "SomeCustomStraight4",           (4, 1, 1)),  # still a straight
        ("Deco",     "DecoArchCurve1",                (1, 1, 1)),
    ])
    def test_footprint(
        self, family: str, name: str, expected: tuple[int, int, int],
    ) -> None:
        g = classify_block(family, name)
        assert (g.footprint_x, g.footprint_y, g.footprint_z) == expected, (
            f"{family}/{name}"
        )

    def test_picks_larger_suffix_when_multiple_words_match(self) -> None:
        # "Slope2Straight4" — "Slope2" matches first, but we want the
        # block's span to reflect the Straight4 (the longer run).
        g = classify_block("Platform", "PlatformPlasticSlope2Straight4")
        assert g.footprint_x == 4


class TestConnectorHint:
    @pytest.mark.parametrize("family,name,expected", [
        ("Road",     "RoadTechStraight",   CONNECTOR_STRAIGHT_X),
        ("Road",     "RoadTechCurve1",     CONNECTOR_CURVE_XZ),
        ("Road",     "RoadDirtSlope2",     CONNECTOR_SLOPE_XY),
        ("Platform", "PlatformPlasticLoop1", CONNECTOR_LOOP_Y),
        ("Platform", "PlatformPlasticGenericPlatform", CONNECTOR_PLATFORM),
        ("Platform", "PlatformPlasticStart", CONNECTOR_ANCHOR),
        ("Gate",     "GateCheckpoint",     CONNECTOR_ANCHOR),
        ("Platform", "PlatformPlasticFinish", CONNECTOR_ANCHOR),
        # Support / deco / gate / unknown — no connector.
        ("Structure", "StructurePillar",   CONNECTOR_NONE),
        ("Gate",      "GateSpecialSlowMotion", CONNECTOR_NONE),
        ("Deco",      "DecoScrapFX1",      CONNECTOR_NONE),
        ("Unknown",   "SomeCustomWackyThing", CONNECTOR_NONE),
    ])
    def test_connector_inference(
        self, family: str, name: str, expected: str,
    ) -> None:
        assert classify_block(family, name).connector_hint == expected


class TestPlacementModeFromCounts:
    @pytest.mark.parametrize("grid,free,expected", [
        (100, 0,   PLACEMENT_GRID_ONLY),
        (0,   50,  PLACEMENT_FREE_ONLY),
        (100, 5,   PLACEMENT_MIXED),
        (5,   100, PLACEMENT_MIXED),
        (0,   0,   PLACEMENT_UNKNOWN),
    ])
    def test_classification(
        self, grid: int, free: int, expected: str,
    ) -> None:
        assert _placement_mode_from_counts(grid, free) == expected

    def test_pure_classifier_leaves_unknown(self) -> None:
        # classify_block doesn't know placement state; that comes from
        # the corpus aggregation. This anchors the expectation so a
        # future regression that tries to infer it from the name
        # (and inevitably gets it wrong on custom blocks) is caught.
        g = classify_block("Road", "RoadTechStraight")
        assert g.placement_mode == PLACEMENT_UNKNOWN
