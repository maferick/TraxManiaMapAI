from __future__ import annotations

from dataclasses import replace

from src.replay.rules import InvalidTimingRule, Severity
from src.replay.telemetry import SampleFrame
from tests.unit._telemetry_builders import make_telemetry, with_samples


def test_monotonic_clean_replay_passes() -> None:
    t = make_telemetry()
    r = InvalidTimingRule().evaluate(t)
    assert r.passed


def test_rejects_backwards_timestamp() -> None:
    base = make_telemetry()
    bad = list(base.samples)
    # Rewind sample 10 by 200ms
    bad[10] = replace(bad[10], time_ms=bad[9].time_ms - 200)
    t = with_samples(base, bad)
    r = InvalidTimingRule().evaluate(t)
    assert not r.passed
    assert r.severity is Severity.REJECT
    assert r.evidence["reason"] == "non_monotonic_timestamps"


def test_warn_on_gap_above_nominal() -> None:
    base = make_telemetry(sample_rate_hz=50)  # nominal 20ms
    bad = list(base.samples)
    # Inject a 150ms gap (7.5x nominal) — above default factor 3.0 but
    # below hard_gap_ms 2000ms
    bad[5] = replace(bad[5], time_ms=bad[4].time_ms + 150)
    # Shift subsequent samples by the same offset so monotonicity holds
    offset = 150 - (bad[5].time_ms - bad[4].time_ms - 150)  # may be zero
    for j in range(6, len(bad)):
        bad[j] = replace(bad[j], time_ms=bad[j].time_ms + 130)  # keep gaps normal
    t = with_samples(base, bad)
    r = InvalidTimingRule().evaluate(t)
    assert not r.passed
    assert r.severity is Severity.WARN
    assert r.evidence["reason"] == "gap_above_nominal"


def test_reject_on_hard_gap() -> None:
    base = make_telemetry(sample_rate_hz=50, duration_ms=60_000)
    bad = list(base.samples)
    # 3-second gap at sample 50
    bad[50] = replace(bad[50], time_ms=bad[49].time_ms + 3_000)
    for j in range(51, len(bad)):
        bad[j] = replace(bad[j], time_ms=bad[j].time_ms + 2_980)
    t = with_samples(base, bad)
    r = InvalidTimingRule().evaluate(t)
    assert not r.passed
    assert r.severity is Severity.REJECT
    assert r.evidence["reason"] == "excessive_gap"
