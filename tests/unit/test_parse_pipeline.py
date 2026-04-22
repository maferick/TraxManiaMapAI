from __future__ import annotations

from src.parsers.pipeline import (
    _waypoint_row,
    direction_to_rotation,
    extract_block_family,
)


class TestExtractBlockFamily:
    def test_leading_camelcase_token(self) -> None:
        assert extract_block_family("PlatformTechLoopEnd") == "Platform"
        assert extract_block_family("RoadIceCurve2") == "Road"
        assert extract_block_family("DecoWallWaterBase") == "Deco"
        assert extract_block_family("GateFinish") == "Gate"

    def test_single_word(self) -> None:
        assert extract_block_family("Grass") == "Grass"

    def test_no_leading_caps_is_unknown(self) -> None:
        assert extract_block_family("lowercase_block") == "Unknown"

    def test_all_caps_is_unknown(self) -> None:
        # All-caps names don't match the heuristic (no lowercase letters
        # after the first capital). Rare in TM2020 but must degrade
        # gracefully.
        assert extract_block_family("XYZ") == "Unknown"

    def test_empty_string_is_unknown(self) -> None:
        assert extract_block_family("") == "Unknown"

    def test_non_string_is_unknown(self) -> None:
        assert extract_block_family(None) == "Unknown"  # type: ignore[arg-type]


class TestDirectionToRotation:
    def test_cardinal_directions(self) -> None:
        assert direction_to_rotation("North") == 0
        assert direction_to_rotation("East") == 1
        assert direction_to_rotation("South") == 2
        assert direction_to_rotation("West") == 3

    def test_none_defaults_to_zero(self) -> None:
        assert direction_to_rotation(None) == 0

    def test_unknown_defaults_to_zero(self) -> None:
        assert direction_to_rotation("Nowhere") == 0


class TestWaypointRow:
    def test_grid_placement_populates_xyz_only(self) -> None:
        row = _waypoint_row(
            map_id=42,
            parser_version="0.1.0",
            waypoint_index=0,
            waypoint={
                "tag": "Checkpoint",
                "order": 0,
                "block_name": "RoadTechCheckpoint",
                "placement": "grid",
                "x": 10, "y": 36, "z": 20,
            },
        )
        # (map_id, parser_version, idx, tag, order, name, placement, x, y, z, ax, ay, az)
        assert row == (
            42, "0.1.0", 0, "Checkpoint", 0, "RoadTechCheckpoint", "grid",
            10, 36, 20, None, None, None,
        )

    def test_free_placement_populates_abs_only(self) -> None:
        row = _waypoint_row(
            map_id=42,
            parser_version="0.1.0",
            waypoint_index=5,
            waypoint={
                "tag": "Goal",
                "order": 0,
                "block_name": "GateFinish",
                "placement": "free",
                "abs_x": 128.5, "abs_y": 9.25, "abs_z": 416.0,
            },
        )
        assert row[7:10] == (None, None, None)
        assert row[10:13] == (128.5, 9.25, 416.0)
        assert row[6] == "free"

    def test_linked_checkpoint_preserves_order(self) -> None:
        row = _waypoint_row(
            map_id=7,
            parser_version="0.1.0",
            waypoint_index=2,
            waypoint={
                "tag": "LinkedCheckpoint",
                "order": 64,
                "block_name": "PlatformTechCheckpoint",
                "placement": "grid",
                "x": 17, "y": 135, "z": 24,
            },
        )
        assert row[3] == "LinkedCheckpoint"
        assert row[4] == 64

    def test_missing_order_defaults_to_zero(self) -> None:
        row = _waypoint_row(
            map_id=1,
            parser_version="0.1.0",
            waypoint_index=0,
            waypoint={
                "tag": "Spawn",
                "block_name": "RoadDirtStart",
                "placement": "grid",
                "x": 27, "y": 10, "z": 29,
            },
        )
        assert row[4] == 0

    def test_unknown_placement_falls_back_to_grid(self) -> None:
        # Defensive: if the wrapper ever emits a placement string we
        # don't know, treat it as grid rather than crashing.
        row = _waypoint_row(
            map_id=1,
            parser_version="0.1.0",
            waypoint_index=0,
            waypoint={
                "tag": "Checkpoint",
                "block_name": "X",
                "placement": "mystery",
                "x": 1, "y": 2, "z": 3,
            },
        )
        assert row[6] == "grid"
        assert row[7:10] == (1, 2, 3)
