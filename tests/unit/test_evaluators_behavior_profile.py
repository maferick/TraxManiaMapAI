"""Unit tests for the behavior_profile evaluator.

Helpers are tested directly; the full :meth:`evaluate` is exercised
through a tiny fake connection + cursor that returns pre-seeded rows,
avoiding the DB-backed integration fixture.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest

from src.evaluation.evaluators.behavior_profile import (
    BehaviorProfileEvaluator,
    _as_float,
    _coeff_of_variation,
    _extract_rule_evidence,
)


# ---------- pure helpers ----------


def test_cv_none_with_one_value() -> None:
    assert _coeff_of_variation([5.0]) is None


def test_cv_none_with_empty() -> None:
    assert _coeff_of_variation([]) is None


def test_cv_none_with_zero_mean() -> None:
    assert _coeff_of_variation([0.0, 0.0]) is None


def test_cv_matches_stdev_over_mean() -> None:
    # Values: 10, 20 → mean=15, stdev≈7.07 → CV≈0.471
    cv = _coeff_of_variation([10.0, 20.0])
    assert cv is not None
    assert abs(cv - 0.4714) < 0.001


def test_as_float_accepts_int() -> None:
    assert _as_float(5) == 5.0


def test_as_float_rejects_bool() -> None:
    assert _as_float(True) is None


def test_as_float_rejects_none_and_strings() -> None:
    assert _as_float(None) is None
    assert _as_float("3.14") is None


def test_extract_rule_evidence_found() -> None:
    diag = {
        "rules": [
            {"name": "x", "evidence": {"a": 1}},
            {"name": "y", "evidence": {"b": 2}},
        ]
    }
    assert _extract_rule_evidence(diag, "y") == {"b": 2}


def test_extract_rule_evidence_missing_returns_none() -> None:
    assert _extract_rule_evidence({"rules": []}, "x") is None
    assert _extract_rule_evidence({}, "x") is None


def test_extract_rule_evidence_handles_malformed() -> None:
    assert _extract_rule_evidence({"rules": "not-a-list"}, "x") is None
    assert _extract_rule_evidence(
        {"rules": [{"name": "x", "evidence": "not-a-dict"}]}, "x"
    ) is None


# ---------- full evaluate() via fake cursor ----------


class _FakeCursor:
    """Mimics just enough of the pymysql cursor context for the
    behavior_profile evaluator (execute + fetchall, nothing else).
    """

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def execute(self, *args: Any, **kwargs: Any) -> None:
        pass

    def fetchall(self) -> list[tuple]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    @contextmanager
    def _cursor(self):
        yield _FakeCursor(self._rows)


def _install_fake_cursor(monkeypatch, rows: list[tuple]) -> _FakeConn:
    """Patch :func:`src.evaluation.evaluators.behavior_profile.cursor`
    to return a fake cursor seeded with ``rows``.
    """
    from src.evaluation.evaluators import behavior_profile as mod
    fake = _FakeConn(rows)
    @contextmanager
    def _fake_cursor(_conn):
        yield _FakeCursor(rows)
    monkeypatch.setattr(mod, "cursor", _fake_cursor)
    return fake


def _diag(
    *,
    signal_source: str = "breadcrumbs",
    inputs_per_second: float | None = 15.0,
    duration_ms: int = 30_000,
    respawn_count: int = 0,
    worst_gap_ms: int = 5_000,
    median_gap_ms: int = 4_000,
) -> str:
    rules = [
        {
            "name": "breadcrumb_spectator",
            "passed": True,
            "evidence": {
                "inputs_per_second": inputs_per_second,
                "duration_ms": duration_ms,
                "inputs_count": 450,
            },
        },
        {
            "name": "breadcrumb_restart",
            "passed": True,
            "evidence": {"respawn_count": respawn_count},
        },
        {
            "name": "breadcrumb_invalid_timing",
            "passed": True,
            "evidence": {
                "worst_gap_ms": worst_gap_ms,
                "median_gap_ms": median_gap_ms,
            },
        },
    ]
    return json.dumps({"status": "clean", "signal_source": signal_source, "rules": rules})


def test_evaluate_emits_flow_score_on_sufficient_replays(monkeypatch) -> None:
    # 4 replays with inputs_per_second: [10, 12, 11, 13] → mean=11.5,
    # stdev≈1.29 → CV≈0.112 → flow_score ≈ 0.888
    rows = [
        (1, _diag(inputs_per_second=10.0)),
        (2, _diag(inputs_per_second=12.0)),
        (3, _diag(inputs_per_second=11.0)),
        (4, _diag(inputs_per_second=13.0)),
    ]
    _install_fake_cursor(monkeypatch, rows)
    ev = BehaviorProfileEvaluator(conn=None, min_replays=3)  # conn unused by fake
    result = ev.evaluate(map_id=42)
    assert result.flow_score is not None
    assert 0.85 < result.flow_score < 0.92
    assert result.diagnostics["replay_count_breadcrumb_eligible"] == 4


def test_evaluate_returns_none_when_fewer_than_min_replays(monkeypatch) -> None:
    rows = [(1, _diag()), (2, _diag())]
    _install_fake_cursor(monkeypatch, rows)
    ev = BehaviorProfileEvaluator(conn=None, min_replays=3)
    result = ev.evaluate(map_id=42)
    assert result.flow_score is None
    assert result.diagnostics["reason"] == "insufficient_breadcrumb_replays"


def test_evaluate_skips_telemetry_signal_source(monkeypatch) -> None:
    rows = [
        (1, _diag(signal_source="telemetry")),
        (2, _diag(signal_source="telemetry")),
        (3, _diag(signal_source="breadcrumbs")),
        (4, _diag(signal_source="breadcrumbs")),
        (5, _diag(signal_source="breadcrumbs")),
    ]
    _install_fake_cursor(monkeypatch, rows)
    ev = BehaviorProfileEvaluator(conn=None, min_replays=3)
    result = ev.evaluate(map_id=42)
    assert result.diagnostics["replay_count_total"] == 5
    assert result.diagnostics["replay_count_breadcrumb_eligible"] == 3
    # telemetry-path replays observed but not scored
    assert "telemetry" in result.diagnostics["signal_sources_observed"]


def test_evaluate_flow_score_near_zero_on_extreme_disagreement(monkeypatch) -> None:
    # Inputs per second: [0.5, 200, 0.5, 200] — stdev=115, mean=100 → CV=1.15 → clipped to 1.0 → flow=0.0
    rows = [
        (1, _diag(inputs_per_second=0.5)),
        (2, _diag(inputs_per_second=200.0)),
        (3, _diag(inputs_per_second=0.5)),
        (4, _diag(inputs_per_second=200.0)),
    ]
    _install_fake_cursor(monkeypatch, rows)
    ev = BehaviorProfileEvaluator(conn=None, min_replays=3)
    result = ev.evaluate(map_id=42)
    # CV >= 1.0 → clip → flow_score = 0.0
    assert result.flow_score == 0.0


def test_evaluate_records_diagnostics_aggregates(monkeypatch) -> None:
    rows = [
        (1, _diag(inputs_per_second=10.0, respawn_count=0, duration_ms=30_000)),
        (2, _diag(inputs_per_second=10.0, respawn_count=2, duration_ms=35_000)),
        (3, _diag(inputs_per_second=10.0, respawn_count=0, duration_ms=40_000)),
    ]
    _install_fake_cursor(monkeypatch, rows)
    ev = BehaviorProfileEvaluator(conn=None, min_replays=3)
    result = ev.evaluate(map_id=42)
    diag = result.diagnostics
    assert diag["inputs_per_second_mean"] == 10.0
    assert diag["inputs_per_second_stdev"] == 0.0
    assert diag["respawn_count_mean"] == pytest.approx(2 / 3, abs=1e-4)
    assert diag["respawn_count_max"] == 2
    assert diag["duration_ms_median"] == 35_000


def test_evaluate_handles_missing_rule_evidence_gracefully(monkeypatch) -> None:
    # Replays whose rule evidence is missing spectator → skipped cleanly
    incomplete_diag = json.dumps(
        {
            "status": "clean",
            "signal_source": "breadcrumbs",
            "rules": [  # no breadcrumb_spectator entry
                {"name": "breadcrumb_restart", "passed": True, "evidence": {"respawn_count": 0}},
            ],
        }
    )
    rows = [
        (1, incomplete_diag),
        (2, _diag(inputs_per_second=10.0)),
        (3, _diag(inputs_per_second=11.0)),
        (4, _diag(inputs_per_second=9.0)),
    ]
    _install_fake_cursor(monkeypatch, rows)
    ev = BehaviorProfileEvaluator(conn=None, min_replays=3)
    result = ev.evaluate(map_id=42)
    # 1 skipped due to missing spectator, 3 eligible — meets threshold
    assert result.flow_score is not None
    assert result.diagnostics["replay_count_breadcrumb_eligible"] == 3


def test_evaluate_handles_invalid_json_rows(monkeypatch) -> None:
    rows = [
        (1, "not valid json"),
        (2, _diag(inputs_per_second=10.0)),
        (3, _diag(inputs_per_second=11.0)),
        (4, _diag(inputs_per_second=12.0)),
    ]
    _install_fake_cursor(monkeypatch, rows)
    ev = BehaviorProfileEvaluator(conn=None, min_replays=3)
    result = ev.evaluate(map_id=42)
    assert result.flow_score is not None
    # Invalid-JSON row silently dropped, 3 eligible remain
    assert result.diagnostics["replay_count_breadcrumb_eligible"] == 3
