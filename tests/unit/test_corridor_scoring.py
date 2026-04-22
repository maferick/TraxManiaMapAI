"""Tests for the pure scoring function (src/corridor/scoring.py)."""
from __future__ import annotations

import pytest

from src.corridor.scoring import (
    SCORE_VERSION,
    EdgeEvidence,
    score_corridor,
    score_edge,
)


def _ev(
    rule_support: bool = True,
    path_support_count: int = 0,
    pattern_weight: float = 0.0,
    negative_evidence_count: int = 0,
) -> EdgeEvidence:
    return EdgeEvidence(
        rule_support=rule_support,
        path_support_count=path_support_count,
        pattern_weight=pattern_weight,
        negative_evidence_count=negative_evidence_count,
    )


class TestScoreEdge:
    def test_rule_support_false_returns_zero(self) -> None:
        ev = _ev(rule_support=False, path_support_count=100,
                 pattern_weight=1.0, negative_evidence_count=0)
        assert score_edge(ev, per_map_max_path_support=100) == 0.0

    def test_baseline_for_pure_rule_support(self) -> None:
        ev = _ev(rule_support=True)
        # baseline + 0 + 0 = 0.5; no deco downweight.
        assert score_edge(ev, per_map_max_path_support=0) == pytest.approx(0.5)

    def test_max_signals_gives_high_score(self) -> None:
        ev = _ev(
            rule_support=True,
            path_support_count=100,
            pattern_weight=1.0,
            negative_evidence_count=0,
        )
        # baseline(0.5) + path(0.3 × 1.0) + pattern(0.2 × 1.0) = 1.0
        # × deco factor 1.0 → 1.0
        assert score_edge(ev, per_map_max_path_support=100) == pytest.approx(1.0)

    def test_full_deco_halves_raw_score(self) -> None:
        ev = _ev(
            rule_support=True,
            path_support_count=100,
            pattern_weight=1.0,
            negative_evidence_count=12,
        )
        # raw 1.0 × deco(1 - 0.5 × 1.0) = 0.5
        assert score_edge(ev, per_map_max_path_support=100) == pytest.approx(0.5)

    def test_path_support_log_normalized(self) -> None:
        # path_support=1 vs 100 with max=100: the log normalization
        # means 1 is nonzero but much smaller than 100.
        weak = _ev(path_support_count=1, pattern_weight=0.0)
        strong = _ev(path_support_count=100, pattern_weight=0.0)
        s_weak = score_edge(weak, per_map_max_path_support=100)
        s_strong = score_edge(strong, per_map_max_path_support=100)
        assert s_weak < s_strong
        assert s_weak > 0.5  # above baseline since some support

    def test_zero_max_path_support_no_boost(self) -> None:
        # per_map_max_path_support=0: can't normalize path boost;
        # should degrade gracefully to baseline + pattern.
        ev = _ev(path_support_count=10, pattern_weight=0.0)
        s = score_edge(ev, per_map_max_path_support=0)
        assert s == pytest.approx(0.5)  # baseline only

    def test_pattern_weight_clamped(self) -> None:
        # Pattern weight > 1.0 shouldn't lift score above budget;
        # < 0 shouldn't go negative.
        high = _ev(pattern_weight=99.0)
        low = _ev(pattern_weight=-1.0)
        assert score_edge(high, per_map_max_path_support=0) <= 1.0
        assert score_edge(low, per_map_max_path_support=0) == pytest.approx(0.5)


class TestScoreCorridor:
    def test_empty_returns_baseline(self) -> None:
        # Self-path (single cell): no edges, baseline score.
        assert score_corridor(
            [], contains_virtual_edge=False, per_map_max_path_support=0,
        ) == 0.5

    def test_single_edge_matches_edge_score(self) -> None:
        ev = _ev(path_support_count=10, pattern_weight=0.5)
        conf = score_corridor(
            [ev], contains_virtual_edge=False, per_map_max_path_support=10,
        )
        expected = score_edge(ev, per_map_max_path_support=10)
        assert conf == pytest.approx(expected)

    def test_weakest_link_wins(self) -> None:
        strong = _ev(path_support_count=100, pattern_weight=1.0)
        weak = _ev(negative_evidence_count=12)  # deco-clustered → 0.25
        conf = score_corridor(
            [strong, weak], contains_virtual_edge=False, per_map_max_path_support=100,
        )
        weak_score = score_edge(weak, per_map_max_path_support=100)
        assert conf == pytest.approx(weak_score)

    def test_any_rule_support_false_kills_corridor(self) -> None:
        good = _ev(path_support_count=100, pattern_weight=1.0)
        bad = _ev(rule_support=False)
        assert score_corridor(
            [good, bad], contains_virtual_edge=False, per_map_max_path_support=100,
        ) == 0.0

    def test_virtual_edge_downweight_applied_once(self) -> None:
        ev = _ev(path_support_count=100, pattern_weight=1.0)
        without = score_corridor(
            [ev, ev, ev], contains_virtual_edge=False,
            per_map_max_path_support=100,
        )
        with_virtual = score_corridor(
            [ev, ev, ev], contains_virtual_edge=True,
            per_map_max_path_support=100,
        )
        # 0.8 factor applies once, not three times.
        assert with_virtual == pytest.approx(without * 0.8)

    def test_virtual_edge_does_not_undo_rule_support_gate(self) -> None:
        bad = _ev(rule_support=False)
        # contains_virtual_edge shouldn't rescue a rule-violating edge.
        assert score_corridor(
            [bad], contains_virtual_edge=True, per_map_max_path_support=0,
        ) == 0.0


class TestScoreVersion:
    def test_score_version_is_semver_shape(self) -> None:
        assert isinstance(SCORE_VERSION, str)
        assert SCORE_VERSION.count(".") == 2
