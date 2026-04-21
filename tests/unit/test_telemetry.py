from __future__ import annotations

import pytest

from src.replay.telemetry import (
    TELEMETRY_SCHEMA_VERSION,
    SampleFrame,
    TelemetryFormatError,
    from_dict,
)
from tests.unit._telemetry_builders import make_telemetry


def test_sample_speed_mps() -> None:
    s = SampleFrame(time_ms=0, x=0, y=0, z=0, vx=3, vy=4, vz=0)
    assert s.speed_mps == pytest.approx(5.0)


def test_duration_and_finished() -> None:
    t = make_telemetry(duration_ms=10_000, sample_rate_hz=50)
    assert t.duration_ms == 10_000
    assert t.finished is True

    unfinished = make_telemetry(duration_ms=10_000, finished=False)
    assert unfinished.finished is False
    assert unfinished.finish_time_ms is None


def test_from_dict_happy_path() -> None:
    payload = {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "source_replay_id": "r1",
        "sample_rate_hz": 50,
        "finish_time_ms": 40,
        "samples": [
            {"time_ms": 0, "x": 0, "y": 0, "z": 0, "vx": 0, "vy": 0, "vz": 0},
            {"time_ms": 20, "x": 1, "y": 0, "z": 0, "vx": 50, "vy": 0, "vz": 0},
            {"time_ms": 40, "x": 2, "y": 0, "z": 0, "vx": 50, "vy": 0, "vz": 0},
        ],
        "checkpoint_sample_indices": [2],
    }
    t = from_dict(payload)
    assert t.source_replay_id == "r1"
    assert len(t.samples) == 3
    assert t.checkpoint_sample_indices == (2,)


def test_from_dict_rejects_missing_top_level() -> None:
    with pytest.raises(TelemetryFormatError, match="samples"):
        from_dict(
            {
                "schema_version": TELEMETRY_SCHEMA_VERSION,
                "source_replay_id": "r",
                "sample_rate_hz": 50,
            }
        )


def test_from_dict_rejects_bad_sample() -> None:
    with pytest.raises(TelemetryFormatError, match="time_ms"):
        from_dict(
            {
                "schema_version": TELEMETRY_SCHEMA_VERSION,
                "source_replay_id": "r",
                "sample_rate_hz": 50,
                "samples": [{"x": 0, "y": 0, "z": 0, "vx": 0, "vy": 0, "vz": 0}],
            }
        )


def test_from_dict_rejects_wrong_schema_version() -> None:
    with pytest.raises(TelemetryFormatError, match="schema_version"):
        from_dict(
            {
                "schema_version": TELEMETRY_SCHEMA_VERSION + 999,
                "source_replay_id": "r",
                "sample_rate_hz": 50,
                "samples": [
                    {"time_ms": 0, "x": 0, "y": 0, "z": 0, "vx": 0, "vy": 0, "vz": 0}
                ],
            }
        )


def test_rejects_out_of_bounds_checkpoint() -> None:
    with pytest.raises(TelemetryFormatError, match="checkpoint"):
        from_dict(
            {
                "schema_version": TELEMETRY_SCHEMA_VERSION,
                "source_replay_id": "r",
                "sample_rate_hz": 50,
                "samples": [
                    {"time_ms": 0, "x": 0, "y": 0, "z": 0, "vx": 0, "vy": 0, "vz": 0}
                ],
                "checkpoint_sample_indices": [5],
            }
        )


def test_empty_samples_rejected() -> None:
    with pytest.raises(TelemetryFormatError, match="non-empty"):
        from_dict(
            {
                "schema_version": TELEMETRY_SCHEMA_VERSION,
                "source_replay_id": "r",
                "sample_rate_hz": 50,
                "samples": [],
            }
        )
