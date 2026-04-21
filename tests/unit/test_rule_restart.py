from __future__ import annotations

from src.replay.rules import RestartRule, Severity
from tests.unit._telemetry_builders import make_telemetry


def test_no_restarts_passes() -> None:
    t = make_telemetry()
    r = RestartRule().evaluate(t)
    assert r.passed


def test_single_restart_warns() -> None:
    t = make_telemetry(restart_indices=(200,))
    r = RestartRule().evaluate(t)
    assert not r.passed
    assert r.severity is Severity.WARN


def test_many_restarts_reject() -> None:
    t = make_telemetry(restart_indices=(100, 200, 300, 400))
    r = RestartRule().evaluate(t)
    assert not r.passed
    assert r.severity is Severity.REJECT


def test_threshold_override() -> None:
    t = make_telemetry(restart_indices=(100, 200, 300, 400))
    r = RestartRule().evaluate(t, {"warn_at_count": 10, "reject_at_count": 20})
    assert r.passed
