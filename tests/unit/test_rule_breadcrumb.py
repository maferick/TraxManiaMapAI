"""Tests for the breadcrumb-only rule set. Each rule gets the same
pass/warn/reject + threshold-override coverage as its telemetry
counterpart."""
from __future__ import annotations

from src.replay.breadcrumbs import InputEvent, ReplayBreadcrumbs
from src.replay.rules.base import Severity
from src.replay.rules.breadcrumb import (
    BreadcrumbIncompleteRule,
    BreadcrumbInvalidTimingRule,
    BreadcrumbRestartRule,
    BreadcrumbSpectatorRule,
)
from tests.unit._breadcrumb_builders import make_breadcrumbs, with_respawns


# ---------- BreadcrumbIncompleteRule ----------


def test_incomplete_pass() -> None:
    r = BreadcrumbIncompleteRule().evaluate(make_breadcrumbs())
    assert r.passed


def test_incomplete_reject_too_few_inputs() -> None:
    bc = make_breadcrumbs(inputs=tuple(), inputs_count=0)
    r = BreadcrumbIncompleteRule().evaluate(bc)
    assert not r.passed
    assert r.severity is Severity.REJECT
    assert r.evidence["reason"] == "too_few_inputs"


def test_incomplete_reject_too_short_no_finish() -> None:
    bc = make_breadcrumbs(
        finish_time_ms=None,
        checkpoint_times_ms=(500, 1_000),  # duration 1s < 5s threshold
    )
    r = BreadcrumbIncompleteRule().evaluate(bc)
    assert not r.passed
    assert r.severity is Severity.REJECT
    assert r.evidence["reason"] == "too_short_no_finish"


def test_incomplete_warn_on_long_but_unfinished() -> None:
    bc = make_breadcrumbs(
        finish_time_ms=None,
        checkpoint_times_ms=(10_000, 20_000, 30_000),  # 30s, no finish
    )
    r = BreadcrumbIncompleteRule().evaluate(bc)
    assert not r.passed
    assert r.severity is Severity.WARN
    assert r.evidence["reason"] == "no_finish_event"


def test_incomplete_threshold_override() -> None:
    bc = make_breadcrumbs(inputs=tuple(), inputs_count=0)
    r = BreadcrumbIncompleteRule().evaluate(bc, {"min_inputs": 0})
    assert r.passed


# ---------- BreadcrumbRestartRule ----------


def test_restart_pass_on_zero_respawns() -> None:
    r = BreadcrumbRestartRule().evaluate(make_breadcrumbs())
    assert r.passed
    assert r.evidence["respawn_count"] == 0


def test_restart_warn_on_single_respawn() -> None:
    bc = with_respawns(make_breadcrumbs(), 1)
    r = BreadcrumbRestartRule().evaluate(bc)
    assert not r.passed
    assert r.severity is Severity.WARN


def test_restart_reject_on_many_respawns() -> None:
    bc = with_respawns(make_breadcrumbs(), 5)
    r = BreadcrumbRestartRule().evaluate(bc)
    assert not r.passed
    assert r.severity is Severity.REJECT
    assert r.evidence["respawn_count"] == 5


# ---------- BreadcrumbSpectatorRule ----------


def test_spectator_pass_on_active_replay() -> None:
    # make_breadcrumbs default: 150 inputs / 60s = 2.5/sec
    r = BreadcrumbSpectatorRule().evaluate(make_breadcrumbs())
    assert r.passed


def test_spectator_reject_on_low_input_density() -> None:
    bc = make_breadcrumbs(
        inputs=tuple(
            InputEvent(time_ms=i * 10_000, kind="SteerTM2020", repr="")
            for i in range(3)
        ),
        inputs_count=3,
        finish_time_ms=60_000,
    )
    r = BreadcrumbSpectatorRule().evaluate(bc)
    assert not r.passed
    assert r.severity is Severity.REJECT
    assert r.evidence["reason"] == "low_input_density"


def test_spectator_pass_when_no_duration() -> None:
    bc = make_breadcrumbs(
        finish_time_ms=None, checkpoint_times_ms=(),
        inputs=tuple(), inputs_count=0,
    )
    r = BreadcrumbSpectatorRule().evaluate(bc)
    # No duration → spectator can't judge; defer to incomplete rule.
    assert r.passed


# ---------- BreadcrumbInvalidTimingRule ----------


def test_invalid_timing_pass_on_monotonic() -> None:
    r = BreadcrumbInvalidTimingRule().evaluate(make_breadcrumbs())
    assert r.passed


def test_invalid_timing_reject_on_non_monotonic() -> None:
    bc = make_breadcrumbs(checkpoint_times_ms=(10_000, 5_000, 30_000))
    r = BreadcrumbInvalidTimingRule().evaluate(bc)
    assert not r.passed
    assert r.severity is Severity.REJECT
    assert r.evidence["reason"] == "non_monotonic_checkpoints"


def test_invalid_timing_reject_on_hard_gap() -> None:
    bc = make_breadcrumbs(
        checkpoint_times_ms=(10_000, 20_000, 200_000),  # 180s gap
        finish_time_ms=200_000,
    )
    r = BreadcrumbInvalidTimingRule().evaluate(bc)
    assert not r.passed
    assert r.severity is Severity.REJECT
    assert r.evidence["reason"] == "excessive_checkpoint_gap"


def test_invalid_timing_warn_on_gap_above_median() -> None:
    # Median gap ~10s, worst ~60s — 6× median, above 5× default factor, below 120s hard cap.
    bc = make_breadcrumbs(
        checkpoint_times_ms=(10_000, 20_000, 30_000, 40_000, 100_000),
        finish_time_ms=100_000,
    )
    r = BreadcrumbInvalidTimingRule().evaluate(bc)
    assert not r.passed
    assert r.severity is Severity.WARN
    assert r.evidence["reason"] == "gap_above_median"


def test_invalid_timing_pass_on_too_few_checkpoints_to_judge() -> None:
    bc = make_breadcrumbs(checkpoint_times_ms=(10_000,))
    r = BreadcrumbInvalidTimingRule().evaluate(bc)
    assert r.passed
    assert r.evidence["reason"] == "too_few_checkpoints_to_judge"
