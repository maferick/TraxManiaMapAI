"""Pure-function tests for :mod:`src.generation.ai_generator`.

Scope: helpers (direction math, scoring, interval walker). The
DB-facing entry point ``generate_ai_map`` is exercised by the
live smoke on map 1212 — this file pins the primitives.
"""
from __future__ import annotations

import pytest

from src.generation.ai_generator import (
    AI_GENERATOR_WEIGHTS,
    _POSITIVE_WEIGHT_KEYS,
    _CandidateBlock,
    _CatalogueEntry,
    _advance,
    _direction_toward,
    _generate_interval,
    _shadow_cells_clear,
    score_candidate,
)
from src.generation.geom_validator import GeometryInfo


def _entry(family: str, name: str, shape: str = "straight") -> _CatalogueEntry:
    return _CatalogueEntry(
        family=family, name=name,
        info=GeometryInfo(
            shape_class=shape, connector_hint="straight_x",
        ),
    )


class TestDirectionMath:
    def test_advance_rotation_0_is_plus_x(self) -> None:
        assert _advance((5, 9, 3), 0) == (6, 9, 3)

    def test_advance_rotation_1_is_plus_z(self) -> None:
        assert _advance((5, 9, 3), 1) == (5, 9, 4)

    def test_advance_rotation_2_is_minus_x(self) -> None:
        assert _advance((5, 9, 3), 2) == (4, 9, 3)

    def test_advance_rotation_3_is_minus_z(self) -> None:
        assert _advance((5, 9, 3), 3) == (5, 9, 2)

    def test_direction_toward_pure_plus_x(self) -> None:
        assert _direction_toward((0, 9, 0), (5, 9, 0)) == 0

    def test_direction_toward_pure_plus_z(self) -> None:
        assert _direction_toward((0, 9, 0), (0, 9, 5)) == 1

    def test_direction_toward_minus_x(self) -> None:
        assert _direction_toward((5, 9, 0), (0, 9, 0)) == 2

    def test_direction_toward_minus_z(self) -> None:
        assert _direction_toward((0, 9, 5), (0, 9, 0)) == 3

    def test_direction_toward_tiebreak_prefers_x(self) -> None:
        # equal X and Z magnitudes → X wins
        assert _direction_toward((0, 9, 0), (5, 9, 5)) == 0


class TestScoreCandidate:
    def test_observed_pair_beats_unknown(self) -> None:
        cand_known = _CandidateBlock(
            family="Road", name="RoadTechStraight",
            cell=(1, 9, 0), rotation=0,
            info=GeometryInfo(shape_class="straight"),
        )
        cand_unknown = _CandidateBlock(
            family="Road", name="RoadTechCurve",
            cell=(1, 9, 0), rotation=0,
            info=GeometryInfo(shape_class="curve"),
        )
        priors = {
            ("Platform", "PlatformPlasticStart"): {
                ("Road", "RoadTechStraight"): 0.8,
            },
        }
        s_known, _ = score_candidate(
            cand=cand_known,
            prev_block=("Platform", "PlatformPlasticStart"),
            pair_priors=priors, path_so_far=[],
            weights=AI_GENERATOR_WEIGHTS,
        )
        s_unknown, _ = score_candidate(
            cand=cand_unknown,
            prev_block=("Platform", "PlatformPlasticStart"),
            pair_priors=priors, path_so_far=[],
            weights=AI_GENERATOR_WEIGHTS,
        )
        assert s_known > s_unknown

    def test_breakdown_keys_match_schema(self) -> None:
        # The schema's ai_score_breakdown enumerates exactly these
        # keys — the scorer must produce all of them or the
        # artifact fails additionalProperties=false on write.
        cand = _CandidateBlock(
            family="Road", name="RoadTechStraight",
            cell=(0, 9, 0), rotation=0,
            info=GeometryInfo(shape_class="straight"),
        )
        _, breakdown = score_candidate(
            cand=cand, prev_block=None, pair_priors={},
            path_so_far=[], weights=AI_GENERATOR_WEIGHTS,
        )
        assert set(breakdown.keys()) == {
            "pair_prior", "triple_prior", "connector",
            "traversability", "sequence",
            "diversity_penalty", "validation_penalty",
        }

    def test_diversity_penalty_grows_with_repetition(self) -> None:
        cand = _CandidateBlock(
            family="Road", name="RoadTechStraight",
            cell=(5, 9, 0), rotation=0,
            info=GeometryInfo(shape_class="straight"),
        )
        path = [
            {"block_family": "Road", "block_name": "RoadTechStraight"}
            for _ in range(4)
        ]
        path.append(
            {"block_family": "Road", "block_name": "RoadTechCurve"},
        )
        _, breakdown = score_candidate(
            cand=cand, prev_block=None, pair_priors={},
            path_so_far=path, weights=AI_GENERATOR_WEIGHTS,
        )
        # 4 of 5 prior blocks are the same → 0.8 penalty.
        assert breakdown["diversity_penalty"] == pytest.approx(0.8)

    def test_first_step_has_zero_pair_prior(self) -> None:
        # prev_block=None → no transition to score.
        cand = _CandidateBlock(
            family="Road", name="RoadTechStraight",
            cell=(1, 9, 0), rotation=0,
            info=GeometryInfo(shape_class="straight"),
        )
        _, breakdown = score_candidate(
            cand=cand, prev_block=None, pair_priors={},
            path_so_far=[], weights=AI_GENERATOR_WEIGHTS,
        )
        assert breakdown["pair_prior"] == 0.0


class TestIntervalWalker:
    """Greedy walk reaches dst within depth and respects occupancy."""

    def _cat(self) -> list[_CatalogueEntry]:
        return [
            _entry("Road", "RoadTechStraight"),
            _entry("Road", "RoadTechCurve", shape="curve"),
        ]

    def test_reaches_destination_on_straight_axis(self) -> None:
        # Spawn at (0,9,0), Goal at (5,9,0); every step advances +X.
        result = _generate_interval(
            src_cell=(0, 9, 0),
            dst_cell=(5, 9, 0),
            src_block=("Platform", "PlatformPlasticStart"),
            catalogue=self._cat(),
            pair_priors={},
            triple_priors=None,
            occupied_cells={(0, 9, 0), (5, 9, 0)},
            max_depth=12,
            weights=AI_GENERATOR_WEIGHTS,
        )
        assert result.reject_reason is None
        # Walk lands adjacent to dst (cheb≤1 triggers arrival).
        assert result.blocks
        last_cell = (
            result.blocks[-1]["x"],
            result.blocks[-1]["y"],
            result.blocks[-1]["z"],
        )
        # Walker stops when NEXT advance would be within cheb=1 of
        # dst, so the last placed block sits one cell short of the
        # destination (that cell is the dst anchor itself).
        assert max(abs(last_cell[0] - 5), abs(last_cell[2])) <= 2

    def test_occupancy_reject(self) -> None:
        # Pre-occupy (1,9,0) so the first step collides.
        result = _generate_interval(
            src_cell=(0, 9, 0),
            dst_cell=(5, 9, 0),
            src_block=("Platform", "PlatformPlasticStart"),
            catalogue=self._cat(),
            pair_priors={},
            triple_priors=None,
            occupied_cells={(0, 9, 0), (1, 9, 0), (5, 9, 0)},
            max_depth=12,
            weights=AI_GENERATOR_WEIGHTS,
        )
        assert result.reject_reason == "no_valid_candidates"

    def test_empty_catalogue_rejects(self) -> None:
        result = _generate_interval(
            src_cell=(0, 9, 0),
            dst_cell=(5, 9, 0),
            src_block=None,
            catalogue=[],
            pair_priors={},
            triple_priors=None,
            occupied_cells={(0, 9, 0), (5, 9, 0)},
            max_depth=12,
            weights=AI_GENERATOR_WEIGHTS,
        )
        assert result.reject_reason == "no_valid_candidates"

    def test_depth_cap_enforced(self) -> None:
        # Destination 100 cells away with a 5-depth cap → exhaust.
        result = _generate_interval(
            src_cell=(0, 9, 0),
            dst_cell=(100, 9, 0),
            src_block=None,
            catalogue=self._cat(),
            pair_priors={},
            triple_priors=None,
            occupied_cells={(0, 9, 0), (100, 9, 0)},
            max_depth=5,
            weights=AI_GENERATOR_WEIGHTS,
        )
        assert result.reject_reason == "beam_exhausted"
        assert len(result.blocks) == 5

    def test_triple_prior_sharpens_pair(self) -> None:
        # Two candidates with equal pair prior. One has an observed
        # triple (prev_prev → prev → cand), the other doesn't. The
        # triple-backed one wins.
        prev_prev = ("Road", "RoadTechRamp")
        prev = ("Road", "RoadTechStraight")
        common_pair_priors = {
            prev: {
                ("Road", "RoadTechCurve1"): 0.4,
                ("Road", "RoadTechCurve2"): 0.4,
            },
        }
        triple_priors = {
            (prev_prev, prev): {("Road", "RoadTechCurve1"): 0.9},
        }
        cand_with_triple = _CandidateBlock(
            family="Road", name="RoadTechCurve1",
            cell=(1, 9, 0), rotation=0,
            info=GeometryInfo(shape_class="curve"),
        )
        cand_without = _CandidateBlock(
            family="Road", name="RoadTechCurve2",
            cell=(1, 9, 0), rotation=0,
            info=GeometryInfo(shape_class="curve"),
        )
        s_with, _ = score_candidate(
            cand=cand_with_triple,
            prev_block=prev, prev_prev_block=prev_prev,
            pair_priors=common_pair_priors,
            triple_priors=triple_priors,
            path_so_far=[], weights=AI_GENERATOR_WEIGHTS,
        )
        s_without, _ = score_candidate(
            cand=cand_without,
            prev_block=prev, prev_prev_block=prev_prev,
            pair_priors=common_pair_priors,
            triple_priors=triple_priors,
            path_so_far=[], weights=AI_GENERATOR_WEIGHTS,
        )
        assert s_with > s_without

    def test_triple_prior_needs_both_prev_and_prev_prev(self) -> None:
        # prev_prev=None → triple tier must be 0 even if triple_priors
        # is populated. Otherwise step 2 would dip into step-3 priors
        # and get nonsensical scores.
        cand = _CandidateBlock(
            family="Road", name="RoadTechCurve1",
            cell=(1, 9, 0), rotation=0,
            info=GeometryInfo(shape_class="curve"),
        )
        _, breakdown = score_candidate(
            cand=cand,
            prev_block=("Road", "RoadTechStraight"),
            prev_prev_block=None,
            pair_priors={},
            triple_priors={(("A", "a"), ("B", "b")): {("Road", "RoadTechCurve1"): 1.0}},
            path_so_far=[], weights=AI_GENERATOR_WEIGHTS,
        )
        assert breakdown["triple_prior"] == 0.0


class TestShadowCellsPenalty:
    def test_unit_footprint_no_penalty(self) -> None:
        cand = _CandidateBlock(
            family="Road", name="RoadTechStraight",
            cell=(5, 9, 7), rotation=0,
            info=GeometryInfo(footprint_x=1),
        )
        assert _shadow_cells_clear(cand=cand, occupied_cells=set()) == 0.0

    def test_clean_shadow_no_penalty(self) -> None:
        # Wall4 at rot=0 extends to (6,9,7)(7,9,7)(8,9,7) — all free.
        cand = _CandidateBlock(
            family="Platform", name="PlatformPlasticWallStraight4",
            cell=(5, 9, 7), rotation=0,
            info=GeometryInfo(footprint_x=4),
        )
        assert _shadow_cells_clear(cand=cand, occupied_cells=set()) == 0.0

    def test_full_collision_penalty_1(self) -> None:
        cand = _CandidateBlock(
            family="Platform", name="PlatformPlasticWallStraight4",
            cell=(5, 9, 7), rotation=0,
            info=GeometryInfo(footprint_x=4),
        )
        # All 3 shadow cells are occupied.
        occ = {(6, 9, 7), (7, 9, 7), (8, 9, 7)}
        assert _shadow_cells_clear(cand=cand, occupied_cells=occ) == 1.0

    def test_partial_collision_penalty(self) -> None:
        cand = _CandidateBlock(
            family="Platform", name="PlatformPlasticWallStraight4",
            cell=(5, 9, 7), rotation=0,
            info=GeometryInfo(footprint_x=4),
        )
        occ = {(7, 9, 7)}   # 1 of 3 shadow cells collides
        assert _shadow_cells_clear(cand=cand, occupied_cells=occ) == pytest.approx(1 / 3)

    def test_shadow_rotates_with_block(self) -> None:
        # Same Wall4 at rot=1 extends to +Z. Occupy only a +Z cell.
        cand = _CandidateBlock(
            family="Platform", name="PlatformPlasticWallStraight4",
            cell=(5, 9, 7), rotation=1,
            info=GeometryInfo(footprint_x=4),
        )
        occ_plus_z = {(5, 9, 8)}
        assert _shadow_cells_clear(cand=cand, occupied_cells=occ_plus_z) > 0
        occ_plus_x = {(6, 9, 7)}  # +X direction isn't the shadow at rot=1
        assert _shadow_cells_clear(cand=cand, occupied_cells=occ_plus_x) == 0.0


class TestPositiveWeightKeys:
    def test_contains_only_positive_signals(self) -> None:
        # ai_confidence divides by this set's sum. Adding a penalty
        # here would regress v0.0's inflated-denominator bug.
        expected = {
            "pair_prior", "triple_prior", "connector",
            "traversability", "sequence",
        }
        assert set(_POSITIVE_WEIGHT_KEYS) == expected

    def test_all_keys_exist_in_weights(self) -> None:
        for k in _POSITIVE_WEIGHT_KEYS:
            assert k in AI_GENERATOR_WEIGHTS


