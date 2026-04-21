from __future__ import annotations

from src.replay.rules import IncompleteRule, Severity
from src.replay.telemetry import SampleFrame
from tests.unit._telemetry_builders import make_telemetry, with_samples


def test_pass_on_finished_replay() -> None:
    t = make_telemetry()
    r = IncompleteRule().evaluate(t)
    assert r.passed


def test_reject_on_too_few_samples() -> None:
    t = make_telemetry(duration_ms=200, sample_rate_hz=50)  # ~11 samples
    r = IncompleteRule().evaluate(t)
    assert not r.passed
    assert r.severity is Severity.REJECT
    assert r.evidence["reason"] == "too_few_samples"


def test_reject_on_too_short_and_no_finish() -> None:
    t = make_telemetry(duration_ms=2_000, finished=False, sample_rate_hz=50)
    # ~101 samples, above min_samples; but below min_duration_ms + no finish
    r = IncompleteRule().evaluate(t)
    assert not r.passed
    assert r.severity is Severity.REJECT
    assert r.evidence["reason"] == "too_short_no_finish"


def test_warn_on_long_but_unfinished() -> None:
    t = make_telemetry(duration_ms=15_000, finished=False)
    r = IncompleteRule().evaluate(t)
    assert not r.passed
    assert r.severity is Severity.WARN
    assert r.evidence["reason"] == "no_finish_event"


def test_threshold_override() -> None:
    t = make_telemetry(duration_ms=200, sample_rate_hz=50)
    r = IncompleteRule().evaluate(t, {"min_samples": 5, "min_duration_ms": 100})
    assert r.passed
