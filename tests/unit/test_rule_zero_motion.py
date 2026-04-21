from __future__ import annotations

from dataclasses import replace

from src.replay.rules import Severity, ZeroMotionRule
from tests.unit._telemetry_builders import make_telemetry, with_samples


def test_constant_motion_passes() -> None:
    t = make_telemetry()
    r = ZeroMotionRule().evaluate(t)
    assert r.passed


def test_short_stall_warns() -> None:
    base = make_telemetry(sample_rate_hz=50, duration_ms=30_000)
    # Freeze 200 samples = 4s of zero motion starting at index 100
    frozen = list(base.samples)
    freeze_pos_x = frozen[99].x
    for i in range(100, 300):
        frozen[i] = replace(frozen[i], x=freeze_pos_x)
    t = with_samples(base, frozen)
    r = ZeroMotionRule().evaluate(t)
    assert not r.passed
    assert r.severity is Severity.WARN


def test_long_stall_rejects() -> None:
    base = make_telemetry(sample_rate_hz=50, duration_ms=60_000)
    frozen = list(base.samples)
    freeze_pos_x = frozen[99].x
    for i in range(100, 800):  # 14s frozen
        frozen[i] = replace(frozen[i], x=freeze_pos_x)
    t = with_samples(base, frozen)
    r = ZeroMotionRule().evaluate(t)
    assert not r.passed
    assert r.severity is Severity.REJECT
