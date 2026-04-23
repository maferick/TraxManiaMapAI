"""Phase-2 PR D — finishability gate tests."""
from __future__ import annotations

import pytest

from src.generation import (
    AI_CONFIDENCE_FLOOR,
    Anchor,
    AssembledRoute,
    AssemblyError,
    ChosenCorridor,
    FinishabilityResult,
    GATE_VERSION,
    IntervalAssembly,
    run_finishability_gate,
)


def _chosen(
    *,
    corridor_id: int = 1,
    length: int = 4,
    expected_time_ms: int = 4267,
    score: float = 0.5,
) -> ChosenCorridor:
    src = Anchor("Spawn", 0, (0, 0, 0))
    dst = Anchor("Goal", 0, (0, 0, length))
    return ChosenCorridor(
        corridor_id=corridor_id, map_id=1,
        src=src, dst=dst,
        path_cells=tuple((0, 0, i) for i in range(length)),
        path_length=length,
        contains_virtual_edge=False,
        corridor_confidence=None,
        learned_corridor_score=score,
        expected_time_ms=expected_time_ms,
    )


def _route(ai_confidence: float, total_time: int = 4267) -> AssembledRoute:
    chosen = _chosen(score=ai_confidence, expected_time_ms=total_time)
    return AssembledRoute(
        map_id=1,
        anchors=(chosen.src, chosen.dst),
        intervals=(IntervalAssembly(
            index=0, src=chosen.src, dst=chosen.dst, chosen=chosen,
        ),),
        cells_total=chosen.path_length,
        estimated_time_ms=total_time,
        ai_confidence=ai_confidence,
    )


class TestGateVerdicts:
    def test_assembly_error_propagates_reject(self) -> None:
        err = AssemblyError(
            reason="chain_broken",
            detail="demo failure",
            interval_index=2,
        )
        r = run_finishability_gate(err)
        assert isinstance(r, FinishabilityResult)
        assert r.route_verified is False
        assert r.reject_reason == "chain_broken"
        assert r.detail == "demo failure"
        # On a reject we deliberately don't surface numeric fields.
        assert r.estimated_time_ms is None
        assert r.ai_confidence is None
        assert r.gate_version == GATE_VERSION

    def test_below_confidence_floor_rejects(self) -> None:
        r = run_finishability_gate(_route(ai_confidence=0.20))
        assert r.route_verified is False
        assert r.reject_reason == "confidence_below_floor"
        # Numeric fields populated so operator sees what happened.
        assert r.ai_confidence == pytest.approx(0.20)
        assert r.estimated_time_ms == 4267
        assert r.detail is not None
        assert "below floor" in r.detail

    def test_exactly_at_floor_passes(self) -> None:
        r = run_finishability_gate(_route(ai_confidence=AI_CONFIDENCE_FLOOR))
        assert r.route_verified is True
        assert r.reject_reason is None

    def test_above_floor_clean_pass(self) -> None:
        r = run_finishability_gate(_route(ai_confidence=0.75))
        assert r.route_verified is True
        assert r.reject_reason is None
        assert r.ai_confidence == pytest.approx(0.75)
        assert r.estimated_time_ms == 4267
        assert r.detail is None

    def test_gate_version_literal(self) -> None:
        # Scope-v0 pins this literal; any drift should break loudly.
        assert GATE_VERSION == "finishability-v0"

    def test_plain_cp_reject_preserves_reason(self) -> None:
        err = AssemblyError(
            reason="plain_cp_not_supported_v0",
            detail="map is plain-CP",
        )
        r = run_finishability_gate(err)
        assert r.reject_reason == "plain_cp_not_supported_v0"
        assert r.route_verified is False
