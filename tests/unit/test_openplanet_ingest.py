"""Tests for linking a ghost capture back to its corpus map.

`maps` has no map_uid column, so the only join available is the
artifact content hash carried in the staged map filename. Getting that
wrong attaches telemetry to the wrong map, which is worse than not
attaching it at all, so the parser refuses anything it cannot confirm.
"""
from __future__ import annotations

from src.ingestion.openplanet_telemetry import _artifact_hash_from_map_file

SHA = "7748cfac3681cfc46799de41e9142be98665bb9bac70c97e2ba9aceff73ac9dc"


def test_extracts_hash_from_windows_style_staged_path():
    assert _artifact_hash_from_map_file(f"AIGoldSet\\{SHA}.Map.Gbx") == SHA


def test_extracts_hash_from_posix_path():
    assert _artifact_hash_from_map_file(f"/maps/AIGoldSet/{SHA}.Map.Gbx") == SHA


def test_bare_hash_filename_is_accepted():
    assert _artifact_hash_from_map_file(f"{SHA}.Map.Gbx") == SHA


def test_generated_map_has_no_corpus_hash():
    # Pilot maps are named by hand and have no corpus counterpart.
    # Returning None keeps them out of `replays` instead of inventing a
    # link.
    assert _artifact_hash_from_map_file("AIPilot\\pilot797.Map.Gbx") is None


def test_non_hex_and_wrong_length_are_rejected():
    assert _artifact_hash_from_map_file("AIGoldSet\\deadbeef.Map.Gbx") is None
    assert _artifact_hash_from_map_file("AIGoldSet\\" + "z" * 64 + ".Map.Gbx") is None


def test_missing_map_file_is_not_an_error():
    assert _artifact_hash_from_map_file(None) is None
    assert _artifact_hash_from_map_file("") is None
