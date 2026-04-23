"""Phase-2 PR D — generated-map JSON schema validator tests."""
from __future__ import annotations

import copy

import pytest

from src.generation import load_schema, validate_generated_map


def _valid_doc() -> dict:
    return {
        "schema_version": "generation-v0",
        "run_id": "0123456789abcdef",
        "generated_at": "2026-04-23T10:15:23+00:00",
        "inputs": {
            "base_map_id": 1042,
            "base_map_source_id": "108079",
            "style_tag_filter": "Tech",
            "difficulty": "medium",
            "random_seed": 42,
        },
        "provenance": {
            "model_hash": "a" * 64,
            "learned_score_version": "time_envelope_v2_weighted@0.1.0",
            "config_hash": "deadbeef",
            "code_version": "abc1234",
            "classification_version": "0.1.0",
        },
        "map": {
            "waypoint_order_style": "linked",
            "interval_count": 1,
            "blocks": [
                {
                    "block_family": "RoadTech",
                    "block_name": "RoadTechStraight",
                    "x": 12, "y": 24, "z": 16,
                    "rotation": 0,
                }
            ],
            "checkpoints": [
                {"waypoint_index": 0, "waypoint_order": 0, "tag": "Spawn",
                 "x": 0, "y": 0, "z": 0},
                {"waypoint_index": 1, "waypoint_order": 0, "tag": "Goal",
                 "x": 0, "y": 0, "z": 3},
            ],
        },
        "route": {
            "intervals": [
                {
                    "index": 0,
                    "src_tag": "Spawn", "src_order": 0,
                    "dst_tag": "Goal", "dst_order": 0,
                    "chosen_corridor_id": 10,
                    "chosen_corridor_score": 0.8,
                    "path_length_cells": 4,
                    "expected_time_ms": 4267,
                }
            ],
            "cells_total": 4,
            "corridors_used": [
                {
                    "corridor_id": 10,
                    "interval_index": 0,
                    "learned_corridor_score": 0.8,
                    "contains_virtual_edge": False,
                    "path_length_cells": 4,
                }
            ],
        },
        "finishability": {
            "route_verified": True,
            "estimated_time_ms": 4267,
            "ai_confidence": 0.8,
            "reject_reason": None,
            "gate_version": "finishability-v0",
        },
    }


class TestSchemaLoad:
    def test_schema_loads(self) -> None:
        s = load_schema()
        assert s["$schema"].startswith("https://json-schema.org/")
        assert s["title"] == "Generated map — v0"


class TestValidator:
    def test_valid_doc_passes(self) -> None:
        assert validate_generated_map(_valid_doc()) is None

    def test_wrong_schema_version_rejected(self) -> None:
        doc = _valid_doc()
        doc["schema_version"] = "generation-v1"
        err = validate_generated_map(doc)
        assert err is not None
        assert "schema_version" in err

    def test_missing_required_field_rejected(self) -> None:
        doc = _valid_doc()
        del doc["provenance"]
        err = validate_generated_map(doc)
        assert err is not None
        assert "provenance" in err

    def test_reject_reason_outside_enum_rejected(self) -> None:
        doc = _valid_doc()
        doc["finishability"]["reject_reason"] = "invented_reason"
        doc["finishability"]["route_verified"] = False
        err = validate_generated_map(doc)
        assert err is not None

    def test_all_canonical_reject_reasons_accepted(self) -> None:
        valid_reasons = [
            "plain_cp_not_supported_v0",
            "missing_corridor_in_interval",
            "chain_broken",
            "empty_corridors",
            "confidence_below_floor",
            "unknown_block",
            "invalid_schema",
        ]
        for reason in valid_reasons:
            doc = _valid_doc()
            doc["finishability"]["reject_reason"] = reason
            doc["finishability"]["route_verified"] = False
            assert validate_generated_map(doc) is None, \
                f"{reason!r} should be accepted"

    def test_gate_version_must_be_literal(self) -> None:
        doc = _valid_doc()
        doc["finishability"]["gate_version"] = "finishability-v1"
        err = validate_generated_map(doc)
        assert err is not None

    def test_difficulty_out_of_enum_rejected(self) -> None:
        doc = _valid_doc()
        doc["inputs"]["difficulty"] = "legendary"
        err = validate_generated_map(doc)
        assert err is not None

    def test_run_id_hex_enforced(self) -> None:
        doc = _valid_doc()
        doc["run_id"] = "not-hex-at-all"
        err = validate_generated_map(doc)
        assert err is not None

    def test_null_reject_reason_accepted_when_verified(self) -> None:
        doc = _valid_doc()
        doc["finishability"]["route_verified"] = True
        doc["finishability"]["reject_reason"] = None
        assert validate_generated_map(doc) is None
