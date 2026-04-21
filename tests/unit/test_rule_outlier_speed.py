from __future__ import annotations

from dataclasses import replace

from src.replay.rules import OutlierSpeedRule, Severity
from tests.unit._telemetry_builders import make_telemetry, with_samples


def test_normal_speed_passes() -> None:
    t = make_telemetry(straight_speed_mps=30.0)
    r = OutlierSpeedRule().evaluate(t)
    assert r.passed


def test_single_spike_warns() -> None:
    base = make_telemetry(straight_speed_mps=30.0)
    bad = list(base.samples)
    # One sample clearly above max_speed_mps=150 but below hard_cap=250
    bad[100] = replace(bad[100], vx=180.0)
    t = with_samples(base, bad)
    r = OutlierSpeedRule().evaluate(t)
    assert not r.passed
    assert r.severity is Severity.WARN


def test_many_spikes_reject() -> None:
    base = make_telemetry(straight_speed_mps=30.0)
    bad = list(base.samples)
    for i in range(100, 110):
        bad[i] = replace(bad[i], vx=180.0)
    t = with_samples(base, bad)
    r = OutlierSpeedRule().evaluate(t)
    assert not r.passed
    assert r.severity is Severity.REJECT
    assert r.evidence["reason"] == "too_many_speed_spikes"


def test_hard_cap_single_sample_reject() -> None:
    base = make_telemetry(straight_speed_mps=30.0)
    bad = list(base.samples)
    bad[100] = replace(bad[100], vx=400.0)  # way beyond hard cap
    t = with_samples(base, bad)
    r = OutlierSpeedRule().evaluate(t)
    assert not r.passed
    assert r.severity is Severity.REJECT
    assert r.evidence["reason"] == "speed_hard_cap_exceeded"
