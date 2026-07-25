"""Unit tests for src.catalogue.loader."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.catalogue.loader import (
    FACE_EAST,
    FACE_NORTH,
    FACE_SOUTH,
    FACE_WEST,
    load_catalogue,
    opposite_face,
    rotate_face,
    rotate_offset,
    rotated_size,
)


def _write_catalogue(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "catalogue.ndjson"
    meta = {"type": "meta", "schema": "block_catalogue_v1"}
    lines = [json.dumps(meta)] + [json.dumps(r) for r in records]
    path.write_text("\n".join(lines), encoding="utf-8")
    (tmp_path / "catalogue.done.json").write_text(
        json.dumps({"blocks_dumped": len(records)}), encoding="utf-8"
    )
    return path


def _block(block_id: str, waypoint: str, clips: dict[str, list[str]]) -> dict:
    return {
        "type": "block",
        "id": block_id,
        "name": block_id,
        "page": "RoadTech/Main/",
        "waypoint": waypoint,
        "is_pillar": False,
        "variants": [
            {
                "kind": "ground",
                "index": 0,
                "size": [1, 1, 1],
                "units": [
                    {
                        "offset": [0, 0, 0],
                        "underground": False,
                        "terrain_modifier": "",
                        "surface": "",
                        "clips": clips,
                    }
                ],
            }
        ],
    }


class TestRotationMath:
    def test_rotate_face_full_circle(self):
        assert rotate_face(FACE_NORTH, 1) == FACE_EAST
        assert rotate_face(FACE_WEST, 1) == FACE_NORTH
        assert rotate_face(FACE_SOUTH, 2) == FACE_NORTH
        for face in range(4):
            assert rotate_face(face, 4) == face

    def test_ingame_calibration_table(self):
        """Pin the RotationCalibration measurement (2026-07-25).

        Isolated RoadTechCurve1 (catalogue local faces n+e) showed
        its dead-end caps on these world directions per rotation.
        This table came from in-game screenshots — if a refactor
        breaks it, the refactor is wrong, not the table.
        """
        from src.catalogue.loader import FACE_DELTAS

        observed = {
            0: {(-1, 0, 0), (0, 0, 1)},
            1: {(-1, 0, 0), (0, 0, -1)},
            2: {(1, 0, 0), (0, 0, -1)},
            3: {(1, 0, 0), (0, 0, 1)},
        }
        for d, expected in observed.items():
            opened = {
                FACE_DELTAS[rotate_face(f, d)]
                for f in (FACE_NORTH, FACE_EAST)
            }
            assert opened == expected, f"rotation {d}: {opened} != {expected}"

    def test_opposite_face(self):
        assert opposite_face(FACE_NORTH) == FACE_SOUTH
        assert opposite_face(FACE_EAST) == FACE_WEST

    def test_rotate_offset_identity(self):
        assert rotate_offset((2, 0, 1), 0, (3, 1, 2)) == (2, 0, 1)

    def test_rotate_offset_quarter_turns_stay_anchored(self):
        size = (2, 1, 3)
        cells = [(x, 0, z) for x in range(2) for z in range(3)]
        for d in range(4):
            rotated = [rotate_offset(c, d, size) for c in cells]
            sx, _, sz = rotated_size(size, d)
            assert len(set(rotated)) == len(cells)
            assert min(c[0] for c in rotated) == 0
            assert min(c[2] for c in rotated) == 0
            assert max(c[0] for c in rotated) == sx - 1
            assert max(c[2] for c in rotated) == sz - 1

    def test_rotate_offset_four_turns_is_identity(self):
        size = (2, 1, 3)
        cell = (1, 0, 2)
        out = cell
        current_size = size
        for _ in range(4):
            out = rotate_offset(out, 1, current_size)
            current_size = rotated_size(current_size, 1)
        assert out == cell


class TestLoadCatalogue:
    def test_round_trip(self, tmp_path):
        path = _write_catalogue(
            tmp_path,
            [_block("RoadTechStraight", "None", {"n": ["RoadTechFC"], "s": ["RoadTechFC"]})],
        )
        blocks = load_catalogue(path)
        assert set(blocks) == {"RoadTechStraight"}
        variant = blocks["RoadTechStraight"].variant("ground", 0)
        assert variant is not None
        ports = variant.side_ports()
        assert {(p.face, p.clip_id) for p in ports} == {
            (FACE_NORTH, "RoadTechFC"),
            (FACE_SOUTH, "RoadTechFC"),
        }

    def test_int_waypoint_normalised(self, tmp_path):
        rec = _block("SomeGate", 2, {"n": ["X"], "s": ["X"]})
        path = _write_catalogue(tmp_path, [rec])
        assert load_catalogue(path)["SomeGate"].waypoint == "Checkpoint"

    def test_missing_done_marker_rejected(self, tmp_path):
        path = _write_catalogue(tmp_path, [])
        (tmp_path / "catalogue.done.json").unlink()
        with pytest.raises(FileNotFoundError):
            load_catalogue(path)

    def test_wrong_schema_rejected(self, tmp_path):
        path = tmp_path / "catalogue.ndjson"
        path.write_text(
            json.dumps({"type": "meta", "schema": "something_else"}),
            encoding="utf-8",
        )
        (tmp_path / "catalogue.done.json").write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError):
            load_catalogue(path)
