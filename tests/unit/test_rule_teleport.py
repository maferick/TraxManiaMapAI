from __future__ import annotations

from dataclasses import replace

from src.replay.rules import Severity, TeleportRule
from tests.unit._telemetry_builders import make_telemetry, with_samples


def test_clean_replay_passes() -> None:
    t = make_telemetry()
    r = TeleportRule().evaluate(t)
    assert r.passed


def test_single_teleport_rejects() -> None:
    base = make_telemetry()
    bad = list(base.samples)
    # Jump sample 100 by 50 meters — well over max_delta_m=10
    bad[100] = replace(bad[100], x=bad[99].x + 50.0)
    t = with_samples(base, bad)
    r = TeleportRule().evaluate(t)
    assert not r.passed
    assert r.severity is Severity.REJECT
    assert r.evidence["reason"] == "teleport_detected"
    assert r.evidence["at_sample_index"] == 100


def test_soft_jump_warns() -> None:
    base = make_telemetry()
    bad = list(base.samples)
    # 7 meter jump — between soft_delta_m=5 and max_delta_m=10
    bad[100] = replace(bad[100], x=bad[99].x + 7.0)
    t = with_samples(base, bad)
    r = TeleportRule().evaluate(t)
    assert not r.passed
    assert r.severity is Severity.WARN


def test_threshold_override_allows_larger_jumps() -> None:
    base = make_telemetry()
    bad = list(base.samples)
    bad[100] = replace(bad[100], x=bad[99].x + 50.0)
    t = with_samples(base, bad)
    r = TeleportRule().evaluate(
        t, {"max_delta_m": 100.0, "soft_delta_m": 60.0}
    )
    assert r.passed
