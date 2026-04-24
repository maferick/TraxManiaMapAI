"""Pure-function tests for :mod:`src.generation.ai_generator`.

Scope: helpers (direction math, scoring, interval walker). The
DB-facing entry point ``generate_ai_map`` is exercised by the
live smoke on map 1212 — this file pins the primitives.
"""
from __future__ import annotations

import pytest

from src.generation.ai_generator import (
    AI_GENERATOR_WEIGHTS,
    _CandidateBlock,
    _CatalogueEntry,
    _advance,
    _direction_toward,
    _generate_interval,
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
            occupied_cells={(0, 9, 0), (100, 9, 0)},
            max_depth=5,
            weights=AI_GENERATOR_WEIGHTS,
        )
        assert result.reject_reason == "beam_exhausted"
        assert len(result.blocks) == 5

    def test_blocks_carry_ai_score_breakdown(self) -> None:
        # Artifact requires ai_score + ai_score_breakdown on
        # synthesised blocks.
        result = _generate_interval(
            src_cell=(0, 9, 0),
            dst_cell=(3, 9, 0),
            src_block=None,
            catalogue=self._cat(),
            pair_priors={},
            occupied_cells={(0, 9, 0), (3, 9, 0)},
            max_depth=12,
            weights=AI_GENERATOR_WEIGHTS,
        )
        assert result.blocks
        for b in result.blocks:
            assert "ai_score" in b
            assert "ai_score_breakdown" in b
            assert isinstance(b["ai_score_breakdown"], dict)
