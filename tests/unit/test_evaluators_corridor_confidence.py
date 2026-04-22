"""Unit tests for route_corridor@0.1.0 via mocked cursor."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from src.evaluation.evaluators.corridor_confidence import CorridorConfidenceEvaluator


class _FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def execute(self, *args: Any, **kwargs: Any) -> None:
        pass

    def fetchall(self) -> list[tuple]:
        return self._rows


def _install_fake_cursor(monkeypatch, rows: list[tuple]) -> None:
    from src.evaluation.evaluators import corridor_confidence as mod

    @contextmanager
    def _fake(_conn):
        yield _FakeCursor(rows)

    monkeypatch.setattr(mod, "cursor", _fake)


def _row(
    src_tag: str = "Spawn",
    src_order: int = 0,
    dst_tag: str = "Goal",
    dst_order: int = 0,
    corridor_confidence: float = 0.7,
    path_length: int = 4,
    contains_virtual_edge: int = 0,
) -> tuple:
    return (
        src_tag, src_order, dst_tag, dst_order,
        corridor_confidence, path_length, contains_virtual_edge,
    )


class TestCorridorConfidenceEvaluator:
    def test_no_corridors_returns_none(self, monkeypatch) -> None:
        _install_fake_cursor(monkeypatch, [])
        ev = CorridorConfidenceEvaluator(conn=None)
        result = ev.evaluate(map_id=42)
        assert result.drivability_score is None
        assert result.diagnostics["interval_count"] == 0
        assert result.diagnostics["reason"] == "no_scored_corridors"

    def test_single_corridor_returns_its_confidence(self, monkeypatch) -> None:
        _install_fake_cursor(monkeypatch, [_row(corridor_confidence=0.9)])
        ev = CorridorConfidenceEvaluator(conn=None)
        result = ev.evaluate(map_id=42)
        assert result.drivability_score == pytest.approx(0.9)
        assert result.diagnostics["interval_count"] == 1
        assert result.diagnostics["corridor_confidence_mean"] == 0.9

    def test_multiple_corridors_return_mean(self, monkeypatch) -> None:
        _install_fake_cursor(monkeypatch, [
            _row(dst_tag="Checkpoint", dst_order=1, corridor_confidence=0.8),
            _row(dst_tag="Goal", dst_order=0, corridor_confidence=0.6),
        ])
        ev = CorridorConfidenceEvaluator(conn=None)
        result = ev.evaluate(map_id=42)
        assert result.drivability_score == pytest.approx(0.7)
        assert result.diagnostics["corridor_confidence_min"] == 0.6
        assert result.diagnostics["corridor_confidence_max"] == 0.8

    def test_diagnostics_include_virtual_edge_fraction(self, monkeypatch) -> None:
        _install_fake_cursor(monkeypatch, [
            _row(corridor_confidence=0.9, contains_virtual_edge=1),
            _row(corridor_confidence=0.8, contains_virtual_edge=0),
            _row(corridor_confidence=0.7, contains_virtual_edge=1),
            _row(corridor_confidence=0.6, contains_virtual_edge=0),
        ])
        ev = CorridorConfidenceEvaluator(conn=None)
        result = ev.evaluate(map_id=42)
        # 2 of 4 have virtual edges
        assert result.diagnostics["virtual_edge_fraction"] == 0.5

    def test_diagnostics_include_path_length_median(self, monkeypatch) -> None:
        _install_fake_cursor(monkeypatch, [
            _row(corridor_confidence=0.9, path_length=2),
            _row(corridor_confidence=0.8, path_length=4),
            _row(corridor_confidence=0.7, path_length=10),
        ])
        ev = CorridorConfidenceEvaluator(conn=None)
        result = ev.evaluate(map_id=42)
        assert result.diagnostics["path_length_median"] == 4

    def test_result_carries_evaluator_identity(self, monkeypatch) -> None:
        _install_fake_cursor(monkeypatch, [_row()])
        ev = CorridorConfidenceEvaluator(conn=None)
        result = ev.evaluate(map_id=42)
        assert result.evaluator_name == "route_corridor"
        assert result.evaluator_version == "0.1.0"

    def test_stdev_computed_when_two_or_more(self, monkeypatch) -> None:
        _install_fake_cursor(monkeypatch, [
            _row(corridor_confidence=0.5),
            _row(corridor_confidence=1.0),
        ])
        ev = CorridorConfidenceEvaluator(conn=None)
        result = ev.evaluate(map_id=42)
        assert "corridor_confidence_stdev" in result.diagnostics

    def test_stdev_omitted_when_single(self, monkeypatch) -> None:
        _install_fake_cursor(monkeypatch, [_row(corridor_confidence=0.5)])
        ev = CorridorConfidenceEvaluator(conn=None)
        result = ev.evaluate(map_id=42)
        assert "corridor_confidence_stdev" not in result.diagnostics
