from __future__ import annotations

from src.replay.rules import Severity, SpectatorRule
from src.replay.telemetry import SampleFrame
from tests.unit._telemetry_builders import make_telemetry, with_samples


def test_moving_replay_passes() -> None:
    t = make_telemetry(duration_ms=10_000, straight_speed_mps=20.0)
    r = SpectatorRule().evaluate(t)
    assert r.passed


def test_stationary_replay_rejects() -> None:
    base = make_telemetry(duration_ms=10_000, sample_rate_hz=50)
    frozen = [
        SampleFrame(time_ms=s.time_ms, x=0, y=0, z=0, vx=0, vy=0, vz=0)
        for s in base.samples
    ]
    t = with_samples(base, frozen)
    r = SpectatorRule().evaluate(t)
    assert not r.passed
    assert r.severity is Severity.REJECT


def test_threshold_override() -> None:
    base = make_telemetry(duration_ms=10_000, sample_rate_hz=50)
    frozen = [
        SampleFrame(time_ms=s.time_ms, x=0, y=0, z=0, vx=0, vy=0, vz=0)
        for s in base.samples
    ]
    t = with_samples(base, frozen)
    r = SpectatorRule().evaluate(t, {"min_total_distance_m": 0.0})
    assert r.passed
