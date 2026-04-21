from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.replay.pipeline import (
    FileTelemetryLoader,
    ReplayRow,
    TelemetryLoadError,
)
from src.replay.telemetry import TELEMETRY_SCHEMA_VERSION


def _row(path: str | None) -> ReplayRow:
    return ReplayRow(
        id=1,
        map_id=1,
        raw_artifact_path=path,
        raw_artifact_hash=None,
        finish_time_ms=30_000,
    )


def _payload() -> dict:
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "source_replay_id": "r",
        "sample_rate_hz": 50,
        "finish_time_ms": 40,
        "samples": [
            {"time_ms": 0, "x": 0, "y": 0, "z": 0, "vx": 0, "vy": 0, "vz": 0},
            {"time_ms": 20, "x": 1, "y": 0, "z": 0, "vx": 50, "vy": 0, "vz": 0},
            {"time_ms": 40, "x": 2, "y": 0, "z": 0, "vx": 50, "vy": 0, "vz": 0},
        ],
    }


def test_load_happy_path(tmp_path: Path) -> None:
    artifact = tmp_path / "raw"
    artifact.write_bytes(b"ignored")
    (tmp_path / "raw.telemetry.json").write_text(json.dumps(_payload()))
    loader = FileTelemetryLoader()
    t = loader.load(_row(str(artifact)))
    assert t.source_replay_id == "r"


def test_no_raw_path_raises(tmp_path: Path) -> None:
    loader = FileTelemetryLoader()
    with pytest.raises(TelemetryLoadError, match="raw_artifact_path"):
        loader.load(_row(None))


def test_missing_sidecar_raises(tmp_path: Path) -> None:
    loader = FileTelemetryLoader()
    with pytest.raises(TelemetryLoadError, match="sidecar missing"):
        loader.load(_row(str(tmp_path / "nonexistent")))


def test_invalid_json_raises(tmp_path: Path) -> None:
    artifact = tmp_path / "raw"
    artifact.write_bytes(b"")
    (tmp_path / "raw.telemetry.json").write_text("{not valid json")
    loader = FileTelemetryLoader()
    with pytest.raises(TelemetryLoadError, match="not valid JSON"):
        loader.load(_row(str(artifact)))
