from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.evaluation import (
    EvaluationResult,
    Evaluator,
    all_registered,
    get,
    register,
)
from src.evaluation.registry import _reset_for_tests


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    _reset_for_tests()


def _make_evaluator_class(name: str, version: str) -> type[Evaluator]:
    class _E(Evaluator):
        pass

    _E.name = name
    _E.version = version
    _E.__abstractmethods__ = frozenset()  # type: ignore[attr-defined]
    return _E


class TestRegister:
    def test_registers_valid_evaluator(self) -> None:
        cls = _make_evaluator_class("flow_surrogate", "1.0.0")
        register(cls)
        assert get("flow_surrogate") is cls
        assert "flow_surrogate" in all_registered()

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValueError, match="name"):
            register(_make_evaluator_class("", "1.0.0"))

    def test_rejects_bad_version(self) -> None:
        with pytest.raises(ValueError):
            register(_make_evaluator_class("bad_ver", "v1"))

    def test_rejects_duplicate_name(self) -> None:
        register(_make_evaluator_class("dup", "1.0.0"))
        with pytest.raises(ValueError, match="already registered"):
            register(_make_evaluator_class("dup", "2.0.0"))

    def test_same_class_is_idempotent(self) -> None:
        cls = _make_evaluator_class("idem", "1.0.0")
        register(cls)
        register(cls)
        assert get("idem") is cls


class TestEvaluationResult:
    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            EvaluationResult(
                map_id=1,
                evaluator_name="e",
                evaluator_version="1.0.0",
                benchmark_set_version=None,
                created_at=datetime(2026, 4, 21, 12, 0, 0),
                code_version=None,
                source_artifact_ids={},
            )

    def test_rejects_bad_evaluator_version(self) -> None:
        with pytest.raises(ValueError):
            EvaluationResult(
                map_id=1,
                evaluator_name="e",
                evaluator_version="not-semver",
                benchmark_set_version=None,
                created_at=datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc),
                code_version=None,
                source_artifact_ids={},
            )

    def test_accepts_minimal_valid(self) -> None:
        r = EvaluationResult(
            map_id=1,
            evaluator_name="e",
            evaluator_version="1.0.0",
            benchmark_set_version="tech-strong-v1",
            created_at=datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc),
            code_version="abc123",
            source_artifact_ids={"map": "parse-v1"},
        )
        assert r.evaluator_version == "1.0.0"
        assert r.diagnostics == {}
