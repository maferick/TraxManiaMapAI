"""Unit tests for route_corridor_learned@0.1.0 via mocked cursor."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from src.evaluation.evaluators.corridor_learned import CorridorLearnedEvaluator


class _FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def execute(self, *args: Any, **kwargs: Any) -> None:
        pass

    def fetchall(self) -> list[tuple]:
        return self._rows


def _install_fake_cursor(monkeypatch, rows: list[tuple]) -> None:
    from src.evaluation.evaluators import corridor_learned as mod

    @contextmanager
    def _fake(_conn):
        yield _FakeCursor(rows)

    monkeypatch.setattr(mod, "cursor", _fake)


def _row(
    src_tag: str = "Spawn",
    src_order: int = 0,
    dst_tag: str = "Goal",
    dst_order: int = 0,
    learned_score: float = 0.7,
    path_length: int = 4,
    contains_virtual_edge: int = 0,
    learned_score_version: str = "time_envelope@0.1.0",
    model_hash: str = "abc" * 21 + "d",
) -> tuple:
    return (
        src_tag, src_order, dst_tag, dst_order,
        learned_score, path_length, contains_virtual_edge,
        learned_score_version, model_hash,
    )


class TestCorridorLearnedEvaluator:
    def test_no_scored_returns_none(self, monkeypatch) -> None:
        _install_fake_cursor(monkeypatch, [])
        ev = CorridorLearnedEvaluator(conn=None)
        result = ev.evaluate(map_id=42)
        assert result.drivability_score is None
        assert result.diagnostics["interval_count"] == 0
        assert result.diagnostics["reason"] == "no_learned_scores"

    def test_single_row_returns_its_score(self, monkeypatch) -> None:
        _install_fake_cursor(monkeypatch, [_row(learned_score=0.9)])
        ev = CorridorLearnedEvaluator(conn=None)
        result = ev.evaluate(map_id=42)
        assert result.drivability_score == pytest.approx(0.9)
        assert result.diagnostics["learned_score_mean"] == 0.9

    def test_multiple_rows_return_mean(self, monkeypatch) -> None:
        _install_fake_cursor(monkeypatch, [
            _row(dst_tag="Checkpoint", dst_order=1, learned_score=0.8),
            _row(dst_tag="Goal", dst_order=0, learned_score=0.6),
        ])
        ev = CorridorLearnedEvaluator(conn=None)
        result = ev.evaluate(map_id=42)
        assert result.drivability_score == pytest.approx(0.7)
        assert result.diagnostics["learned_score_min"] == 0.6
        assert result.diagnostics["learned_score_max"] == 0.8

    def test_provenance_exposed(self, monkeypatch) -> None:
        _install_fake_cursor(monkeypatch, [
            _row(
                learned_score=0.9,
                learned_score_version="time_envelope@0.1.0",
                model_hash="f" * 64,
            ),
        ])
        ev = CorridorLearnedEvaluator(conn=None)
        result = ev.evaluate(map_id=42)
        assert result.diagnostics["learned_score_version"] == "time_envelope@0.1.0"
        assert result.diagnostics["model_hash"] == "f" * 12

    def test_result_carries_evaluator_identity(self, monkeypatch) -> None:
        _install_fake_cursor(monkeypatch, [_row()])
        ev = CorridorLearnedEvaluator(conn=None)
        result = ev.evaluate(map_id=42)
        assert result.evaluator_name == "route_corridor_learned"
        assert result.evaluator_version == "0.1.0"

    def test_stdev_computed_when_two_or_more(self, monkeypatch) -> None:
        _install_fake_cursor(monkeypatch, [
            _row(learned_score=0.5),
            _row(learned_score=1.0),
        ])
        ev = CorridorLearnedEvaluator(conn=None)
        result = ev.evaluate(map_id=42)
        assert "learned_score_stdev" in result.diagnostics

    def test_stdev_omitted_when_single(self, monkeypatch) -> None:
        _install_fake_cursor(monkeypatch, [_row(learned_score=0.5)])
        ev = CorridorLearnedEvaluator(conn=None)
        result = ev.evaluate(map_id=42)
        assert "learned_score_stdev" not in result.diagnostics

    def test_virtual_edge_fraction(self, monkeypatch) -> None:
        _install_fake_cursor(monkeypatch, [
            _row(learned_score=0.5, contains_virtual_edge=1),
            _row(learned_score=0.5, contains_virtual_edge=0),
        ])
        ev = CorridorLearnedEvaluator(conn=None)
        result = ev.evaluate(map_id=42)
        assert result.diagnostics["virtual_edge_fraction"] == 0.5
