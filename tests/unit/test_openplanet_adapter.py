"""Tests for the AIReplayTelemetry rig-output adapter.

These run offline. The point is that a rig document can be validated
without TM2020 in the loop, so a schema regression is caught by the test
suite rather than by a failed 763-map batch.
"""
from __future__ import annotations

import math

import pytest

from src.replay.openplanet_adapter import (
    RIG_PROTOCOL,
    RigOutputError,
    telemetry_from_rig_output,
    telemetry_to_dict,
)
from src.replay.telemetry import from_dict as telemetry_from_dict


def _frames(count: int, *, period_ms: int = 50, speed_mps: float = 10.0):
    """A ghost sitting still for 4 frames, then moving +x at `speed_mps`."""
    frames = []
    x = 0.0
    for i in range(count):
        if i >= 4:
            x += speed_mps * (period_ms / 1000.0)
        frames.append({"t_ms": i * period_ms, "x": x, "y": 9.0, "z": 0.0})
    return frames


def _doc(**overrides):
    doc = {
        "protocol": RIG_PROTOCOL,
        "job_id": 1,
        "run_id": "run",
        "load_success": True,
        "plugin_version": "replay-plugin-v0.2",
        "sample_period_ms": 50,
        "finished": True,
        "exit_reason": "finished",
        "frame_count": 20,
        "start_frame_index": 4,
        "ghost": {
            "nickname": "author",
            "time_ms": 800,
            "nb_respawns": 0,
            "checkpoint_times_ms": [200, 600],
        },
        "frames": _frames(20),
    }
    doc.update(overrides)
    return doc


def test_adapts_and_round_trips_through_canonical_parser():
    telemetry = telemetry_from_rig_output(_doc(), source_replay_id="r1")

    assert telemetry.schema_version == 1
    assert telemetry.sample_rate_hz == 20
    # 20 frames minus the 4 pre-spawn nulls ahead of the anchor.
    assert len(telemetry.samples) == 16
    assert telemetry.samples[0].time_ms == 0
    assert telemetry.extra["trimmed_lead_frames"] == 4
    assert telemetry.extra["clock_rebased_to_race_start"] is True
    assert telemetry.finish_time_ms == 800
    # A ghost has no login, only a nickname. Passing the nickname off as
    # a login would corrupt a field downstream code treats as identity.
    assert telemetry.player_login is None
    assert telemetry.extra["ghost_nickname"] == "author"

    telemetry_from_dict(telemetry_to_dict(telemetry))


def test_velocity_is_differentiated_from_position():
    telemetry = telemetry_from_rig_output(_doc(), source_replay_id="r1")

    # Central difference recovers the 10 m/s we injected.
    assert telemetry.samples[5].vx == pytest.approx(10.0, abs=1e-6)
    assert telemetry.samples[5].speed_mps == pytest.approx(10.0, abs=1e-6)


def test_pre_spawn_nulls_are_trimmed_not_differentiated_across():
    """The regression that produced 31,081 km/h on a real capture.

    Ghost_GetPosition returns the origin until the ghost is placed in
    the scene. Differentiating across that boundary invents a velocity
    orders of magnitude past anything drivable.
    """
    period = 50
    frames = [{"t_ms": i * period, "x": 0.0, "y": 0.0, "z": 0.0}
              for i in range(6)]
    # Ghost pops into the world 880m away, then creeps forward.
    for i in range(6, 12):
        frames.append({
            "t_ms": i * period,
            "x": 880.0 + (i - 6) * 0.5,
            "y": 234.0,
            "z": 656.0,
        })
    telemetry = telemetry_from_rig_output(
        _doc(frames=frames, start_frame_index=6, ghost={"time_ms": 300,
             "checkpoint_times_ms": [], "nickname": "n", "nb_respawns": 0}),
        source_replay_id="r1",
    )

    assert len(telemetry.samples) == 6
    assert telemetry.samples[0].x == pytest.approx(880.0)
    # 0.5m per 50ms = 10 m/s. Nothing near the 8633 m/s artifact.
    assert max(s.speed_mps for s in telemetry.samples) < 20.0


def test_checkpoints_anchor_to_observed_movement_not_frame_zero():
    telemetry = telemetry_from_rig_output(_doc(), source_replay_id="r1")

    # After trimming to the anchor, the clock reads 0 at race start, so
    # splits at 200ms and 600ms land on samples 4 and 12.
    assert telemetry.checkpoint_sample_indices == (4, 12)
    assert telemetry.samples[4].time_ms == 200
    assert telemetry.samples[12].time_ms == 600


def test_missing_movement_anchor_drops_checkpoints_rather_than_guessing():
    telemetry = telemetry_from_rig_output(
        _doc(start_frame_index=-1), source_replay_id="r1"
    )
    # Anchoring to frame 0 would silently misattribute every split.
    assert telemetry.checkpoint_sample_indices == ()
    assert telemetry.extra["ghost_checkpoint_times_ms"] == [200, 600]
    # Unanchored means untrimmed and un-rebased: nothing is guessed.
    assert len(telemetry.samples) == 20
    assert telemetry.extra["clock_rebased_to_race_start"] is False


def test_out_of_range_anchor_falls_back_to_unanchored():
    telemetry = telemetry_from_rig_output(
        _doc(start_frame_index=999), source_replay_id="r1"
    )
    assert len(telemetry.samples) == 20
    assert telemetry.checkpoint_sample_indices == ()
    assert telemetry.extra["clock_rebased_to_race_start"] is False


def test_unfinished_run_has_no_finish_time():
    telemetry = telemetry_from_rig_output(
        _doc(finished=False, exit_reason="playback_timeout"),
        source_replay_id="r1",
    )
    assert telemetry.finish_time_ms is None
    assert telemetry.finished is False


def test_repeated_timestamps_do_not_divide_by_zero():
    frames = [
        {"t_ms": 0, "x": 0.0, "y": 9.0, "z": 0.0},
        {"t_ms": 0, "x": 0.0, "y": 9.0, "z": 0.0},
        {"t_ms": 0, "x": 0.0, "y": 9.0, "z": 0.0},
    ]
    telemetry = telemetry_from_rig_output(
        _doc(frames=frames, start_frame_index=-1), source_replay_id="r1"
    )
    assert all(math.isfinite(s.vx) for s in telemetry.samples)
    assert telemetry.samples[1].speed_mps == pytest.approx(0.0)


def test_failed_job_is_rejected_with_the_plugin_reason():
    doc = _doc(load_success=False, load_error="Replay_Load failed: x / y")
    with pytest.raises(RigOutputError, match="Replay_Load failed"):
        telemetry_from_rig_output(doc, source_replay_id="r1")


def test_zero_frame_job_is_rejected_and_names_the_exit_reason():
    doc = _doc(frames=[], exit_reason="ghost_never_moved")
    with pytest.raises(RigOutputError, match="ghost_never_moved"):
        telemetry_from_rig_output(doc, source_replay_id="r1")


def test_wrong_protocol_is_rejected():
    with pytest.raises(RigOutputError, match="protocol"):
        telemetry_from_rig_output(
            _doc(protocol="something_else"), source_replay_id="r1"
        )
