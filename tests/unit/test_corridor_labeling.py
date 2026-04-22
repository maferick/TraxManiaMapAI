"""Tests for the pure edge-labeling function + LabelingStats helpers.

The :class:`TraversabilityLabeler` DB orchestration is exercised via
integration tests once a Neo4j fixture is available; the unit layer
here tests the state-precedence rules and the stats-math.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.corridor.traversability import (
    STATE_SEED_VALID,
    STATE_UNKNOWN,
    STATE_UNSUPPORTED,
    LabelingStats,
    label_edge,
)


class TestLabelEdgeBothDrivable:
    """Drivable on both sides → seed_valid + rule_support."""

    @pytest.mark.parametrize(
        "src,dst",
        [
            ("Platform", "Platform"),
            ("Road", "Road"),
            ("Platform", "Road"),     # cross-family within DRIVABLE
            ("Road", "Track"),
            ("Gate", "Road"),          # Gate admitted at family level per design
            ("Technics", "Platform"),
            ("Snow", "Road"),
            ("Dirt", "Road"),
        ],
    )
    def test_seed_valid_and_rule_support(self, src: str, dst: str) -> None:
        label = label_edge(src, dst)
        assert label.state == STATE_SEED_VALID
        assert label.rule_support is True


class TestLabelEdgeNonDrivableWins:
    """NON_DRIVABLE on either side closes the edge, regardless of the
    other side. This precedence is load-bearing: a NON_DRIVABLE ∪
    AMBIGUOUS edge must be unsupported, not unknown."""

    @pytest.mark.parametrize(
        "src,dst",
        [
            ("Deco", "Deco"),
            ("Deco", "Platform"),
            ("Platform", "Deco"),
            ("Structure", "Road"),
            ("Water", "Gate"),
            ("Unknown", "Platform"),   # custom-block source
            ("Road", "Unknown"),
            ("Deco", "Open"),           # NON_DRIVABLE + AMBIGUOUS → unsupported
            ("Open", "Stadium"),
        ],
    )
    def test_unsupported_no_rule_support(self, src: str, dst: str) -> None:
        label = label_edge(src, dst)
        assert label.state == STATE_UNSUPPORTED
        assert label.rule_support is False


class TestLabelEdgeAmbiguous:
    """AMBIGUOUS ∪ (DRIVABLE or AMBIGUOUS) → unknown.
    No NON_DRIVABLE side, no two-DRIVABLE combination."""

    @pytest.mark.parametrize(
        "src,dst",
        [
            ("Open", "Open"),
            ("Open", "Platform"),
            ("Platform", "Open"),
            ("Stage", "Road"),
            ("Items", "Gate"),
            ("Nations", "Track"),
            ("Open", "Stage"),          # two ambiguous
        ],
    )
    def test_unknown_no_rule_support(self, src: str, dst: str) -> None:
        label = label_edge(src, dst)
        assert label.state == STATE_UNKNOWN
        assert label.rule_support is False


class TestLabelEdgeUnseenFamily:
    """Unseen families default to AMBIGUOUS per classify_family, so an
    unseen-vs-drivable edge is unknown; unseen-vs-unseen is also unknown;
    unseen-vs-non-drivable is unsupported."""

    def test_unseen_vs_drivable_is_unknown(self) -> None:
        assert label_edge("SomeFutureFamily", "Platform").state == STATE_UNKNOWN

    def test_unseen_vs_unseen_is_unknown(self) -> None:
        assert label_edge("FutureA", "FutureB").state == STATE_UNKNOWN

    def test_unseen_vs_non_drivable_is_unsupported(self) -> None:
        assert label_edge("FutureFamily", "Deco").state == STATE_UNSUPPORTED


class TestLabelEdgeDefensive:
    """Edge cases: empty strings, None-ish inputs. Must not crash — the
    Neo4j query may return a null family on a pathologically-keyed
    Block node; label_edge has to degrade to AMBIGUOUS, which combines
    per the standard rules."""

    def test_empty_strings(self) -> None:
        # Both empty → AMBIGUOUS + AMBIGUOUS → unknown
        assert label_edge("", "").state == STATE_UNKNOWN

    def test_empty_vs_drivable(self) -> None:
        assert label_edge("", "Platform").state == STATE_UNKNOWN

    def test_empty_vs_non_drivable(self) -> None:
        assert label_edge("", "Deco").state == STATE_UNSUPPORTED


class TestLabelingStats:
    """Fractions are derived from the three counters; must behave
    sanely at zero and mid-range."""

    def _stats(self, **kwargs: int) -> LabelingStats:
        s = LabelingStats(started_at=datetime.now(tz=timezone.utc))
        for k, v in kwargs.items():
            setattr(s, k, v)
        return s

    def test_zero_edges_has_zero_fractions(self) -> None:
        s = self._stats()
        assert s.edges_seen == 0
        assert s.suppression_fraction == 0.0
        assert s.unsupported_fraction == 0.0

    def test_all_unsupported(self) -> None:
        s = self._stats(edges_seen=100, unsupported=100)
        assert s.unsupported_fraction == 1.0
        assert s.suppression_fraction == 1.0

    def test_all_seed_valid(self) -> None:
        s = self._stats(edges_seen=100, seed_valid=100)
        assert s.suppression_fraction == 0.0
        assert s.unsupported_fraction == 0.0

    def test_mixed_distribution(self) -> None:
        s = self._stats(edges_seen=10, seed_valid=2, unsupported=6, unknown=2)
        assert s.unsupported_fraction == 0.6
        assert s.suppression_fraction == 0.8  # (6 + 2) / 10

    def test_summary_json_shape(self) -> None:
        s = self._stats(edges_seen=4, seed_valid=1, unsupported=2, unknown=1)
        payload = s.to_summary_json()
        assert payload["edges_seen"] == 4
        assert payload["seed_valid"] == 1
        assert payload["unsupported"] == 2
        assert payload["unknown"] == 1
        assert payload["suppression_fraction"] == 0.75
        assert payload["unsupported_fraction"] == 0.5
