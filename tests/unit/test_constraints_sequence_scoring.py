"""Phase 2 #218-4 — unit tests for sequence scoring."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.constraints import sequence_scoring as ss
from src.constraints.block_geometry import (
    BlockGeometry,
    SHAPE_CHECKPOINT,
    SHAPE_CURVE,
    SHAPE_DECO,
    SHAPE_PLATFORM,
    SHAPE_RAMP,
    SHAPE_START,
    SHAPE_STRAIGHT,
    SHAPE_UNKNOWN,
)
from src.constraints.sequence_scoring import (
    _combine,
    _geometry_score_for,
    _rarity_bucket,
    score_pair,
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _geom(
    family: str = "F", name: str = "B", *,
    shape: str = SHAPE_STRAIGHT, surface: str = "road",
    is_anchor: bool = False, is_deco: bool = False,
) -> BlockGeometry:
    return BlockGeometry(
        block_family=family, block_name=name,
        shape_class=shape, surface_hint=surface,
        is_anchor_capable=is_anchor, is_deco=is_deco,
    )


# ---------------------------------------------------------------------
# Rarity bucketing
# ---------------------------------------------------------------------

class TestRarityBucket:
    def test_unseen_when_count_zero(self) -> None:
        assert _rarity_bucket(0.0, 0) == "unseen"

    def test_common_at_20pct(self) -> None:
        assert _rarity_bucket(0.25, 10) == "common"

    def test_uncommon_at_10pct(self) -> None:
        assert _rarity_bucket(0.10, 5) == "uncommon"

    def test_rare_at_2pct(self) -> None:
        assert _rarity_bucket(0.02, 1) == "rare"


# ---------------------------------------------------------------------
# Geometry score
# ---------------------------------------------------------------------

class TestGeometryScore:
    def test_same_surface_same_shape_pair_scores_high(self) -> None:
        a = _geom(shape=SHAPE_STRAIGHT, surface="road")
        b = _geom(shape=SHAPE_STRAIGHT, surface="road")
        score, detail = _geometry_score_for(a, b)
        # 0.4 base + 0.25 surface + 0.30 shape-pair = 0.95
        assert score == pytest.approx(0.95)
        assert "surface=road" in detail
        assert "shape_pair=straight→straight" in detail

    def test_different_surface_drivable_shapes(self) -> None:
        a = _geom(shape=SHAPE_RAMP, surface="road")
        b = _geom(shape=SHAPE_STRAIGHT, surface="dirt")
        score, _ = _geometry_score_for(a, b)
        # 0.4 base + 0.30 shape-pair (ramp→straight) = 0.70
        assert score == pytest.approx(0.70)

    def test_deco_incurs_penalty(self) -> None:
        a = _geom(shape=SHAPE_STRAIGHT, surface="road", is_deco=False)
        b = _geom(shape=SHAPE_DECO, surface="", is_deco=True)
        score, detail = _geometry_score_for(a, b)
        # 0.4 base - 0.35 deco = 0.05. No surface match, no shape pair.
        assert score == pytest.approx(0.05)
        assert "has-deco" in detail

    def test_unknown_shape_is_penalised(self) -> None:
        a = _geom(shape=SHAPE_UNKNOWN, surface="road")
        b = _geom(shape=SHAPE_STRAIGHT, surface="road")
        score, detail = _geometry_score_for(a, b)
        # 0.4 base + 0.25 surface - 0.10 unknown-shape = 0.55
        assert score == pytest.approx(0.55)
        assert "has-unknown-shape" in detail

    def test_both_none_is_zero(self) -> None:
        score, detail = _geometry_score_for(None, None)
        assert score == 0.0
        assert "uncatalogued" in detail

    def test_one_none_is_uncertain(self) -> None:
        score, detail = _geometry_score_for(None, _geom())
        assert score == 0.15
        assert "one-block" in detail

    def test_anchor_pair_bonus_applies(self) -> None:
        # Start → Straight is a listed compatible pair.
        a = _geom(shape=SHAPE_START, surface="platform", is_anchor=True)
        b = _geom(shape=SHAPE_STRAIGHT, surface="platform")
        score, detail = _geometry_score_for(a, b)
        # 0.4 + 0.25 (surface platform) + 0.30 (shape pair) = 0.95
        assert score == pytest.approx(0.95)

    def test_clamps_to_one(self) -> None:
        # Even if every bonus applied, we never exceed 1.0.
        score, _ = _geometry_score_for(
            _geom(shape=SHAPE_STRAIGHT, surface="road"),
            _geom(shape=SHAPE_STRAIGHT, surface="road"),
        )
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------
# Combine
# ---------------------------------------------------------------------

class TestCombine:
    def test_default_weights(self) -> None:
        # alpha=0.55 pattern + beta=0.45 geometry → combined
        c = _combine(0.6, 0.8, alpha=0.55, beta=0.45)
        # (0.55*0.6 + 0.45*0.8) / 1.0 = 0.33 + 0.36 = 0.69
        assert c == pytest.approx(0.69)

    def test_zero_weights_is_zero(self) -> None:
        assert _combine(0.9, 0.9, alpha=0.0, beta=0.0) == 0.0

    def test_pure_pattern(self) -> None:
        assert _combine(0.7, 0.3, alpha=1.0, beta=0.0) == pytest.approx(0.7)


# ---------------------------------------------------------------------
# score_pair — end-to-end with monkeypatched DB
# ---------------------------------------------------------------------

def _stub_pair_and_marginal(
    transition_count: int, marginal_total: int,
    geom_a: BlockGeometry | None = None,
    geom_b: BlockGeometry | None = None,
):
    """Return a (cursor-factory, fetch_geometry-factory) usable
    for monkeypatching. The cursor returns pair/marginal rows in
    order of the SQLs that score_pair issues."""
    cur = MagicMock()
    # Two fetchone calls in _fetch_pattern_score: pair lookup, marginal.
    cur.fetchone.side_effect = [
        (transition_count,),       # pair
        (marginal_total,),         # marginal
    ]
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    ctx.__exit__.return_value = False

    def fake_cursor(_conn):
        return ctx

    def fake_fetch_geometry(_conn, family, name):
        if family == "A":
            return geom_a
        if family == "B":
            return geom_b
        return None

    return fake_cursor, fake_fetch_geometry


class TestScorePair:
    def test_happy_common_pair(self, monkeypatch) -> None:
        # Pattern seen 50/100 times (common), shapes compatible.
        fake_cursor, fake_fetch = _stub_pair_and_marginal(
            transition_count=50, marginal_total=100,
            geom_a=_geom("A", "AName", shape=SHAPE_STRAIGHT, surface="road"),
            geom_b=_geom("B", "BName", shape=SHAPE_CURVE, surface="road"),
        )
        monkeypatch.setattr(ss, "cursor", fake_cursor)
        monkeypatch.setattr(ss, "fetch_geometry", fake_fetch)

        result = score_pair(
            MagicMock(),
            a_family="A", a_name="AName",
            b_family="B", b_name="BName",
            environment="Stadium2020",
        )
        assert result.pattern_score == pytest.approx(0.5)
        assert result.transition_count == 50
        assert result.marginal_total == 100
        assert result.pattern_rarity == "common"
        # Geometry: 0.4 + 0.25 (surface road) + 0.30 (shape pair) = 0.95
        assert result.geometry_score == pytest.approx(0.95)
        # Combined: 0.55*0.5 + 0.45*0.95 = 0.275 + 0.4275 = 0.7025
        assert result.combined_score == pytest.approx(0.7025)
        assert "pattern=0.500" in result.reasoning
        assert "common" in result.reasoning

    def test_unseen_pair(self, monkeypatch) -> None:
        fake_cursor, fake_fetch = _stub_pair_and_marginal(
            transition_count=0, marginal_total=50,
            geom_a=_geom("A", "AName"),
            geom_b=_geom("B", "BName"),
        )
        monkeypatch.setattr(ss, "cursor", fake_cursor)
        monkeypatch.setattr(ss, "fetch_geometry", fake_fetch)

        r = score_pair(
            MagicMock(),
            a_family="A", a_name="AName",
            b_family="B", b_name="BName",
            environment="Stadium2020",
        )
        assert r.pattern_score == 0.0
        assert r.pattern_rarity == "unseen"
        # Geometry still scores (shapes knowable independently).
        assert r.geometry_score > 0
        # Combined drops correspondingly.
        assert r.combined_score < r.geometry_score

    def test_rare_but_geometrically_plausible(self, monkeypatch) -> None:
        # 1 transition out of 100 — rare — but shapes fit.
        fake_cursor, fake_fetch = _stub_pair_and_marginal(
            transition_count=1, marginal_total=100,
            geom_a=_geom("A", "AName", shape=SHAPE_STRAIGHT, surface="road"),
            geom_b=_geom("B", "BName", shape=SHAPE_STRAIGHT, surface="road"),
        )
        monkeypatch.setattr(ss, "cursor", fake_cursor)
        monkeypatch.setattr(ss, "fetch_geometry", fake_fetch)

        r = score_pair(
            MagicMock(),
            a_family="A", a_name="AName",
            b_family="B", b_name="BName",
            environment="Stadium2020",
        )
        assert r.pattern_rarity == "rare"
        assert r.geometry_score == pytest.approx(0.95)
        # Combined is pulled down by low pattern but stays non-zero.
        assert 0.3 < r.combined_score < 0.6

    def test_marginal_zero_means_score_zero(self, monkeypatch) -> None:
        # No transitions observed starting from A → marginal is 0 →
        # the divide guard returns 0.0 rather than crashing.
        fake_cursor, fake_fetch = _stub_pair_and_marginal(
            transition_count=0, marginal_total=0,
            geom_a=None, geom_b=None,
        )
        monkeypatch.setattr(ss, "cursor", fake_cursor)
        monkeypatch.setattr(ss, "fetch_geometry", fake_fetch)

        r = score_pair(
            MagicMock(),
            a_family="A", a_name="AName",
            b_family="B", b_name="BName",
            environment="Stadium2020",
        )
        assert r.pattern_score == 0.0
        assert r.marginal_total == 0
        assert r.pattern_rarity == "unseen"
