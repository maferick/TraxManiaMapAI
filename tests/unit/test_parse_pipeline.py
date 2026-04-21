from __future__ import annotations

from src.parsers.pipeline import direction_to_rotation, extract_block_family


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
