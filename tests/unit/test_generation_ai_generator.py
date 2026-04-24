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
    _ShapeSurface,
    _advance,
    _direction_toward,
    _generate_interval,
    _sequence_pair_score,
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


class TestSequencePairScore:
    """v0.3 sequence tier — shape+surface compatibility in-memory."""

    def test_same_surface_same_shape_family_high_score(self) -> None:
        # straight → straight on the same surface is the canonical
        # "clean continuation" — scores high.
        prev = _ShapeSurface(shape_class="straight", surface_hint="road_tech")
        cand = _ShapeSurface(shape_class="straight", surface_hint="road_tech")
        assert _sequence_pair_score(prev, cand) == pytest.approx(1.0)

    def test_compatible_shape_different_surface(self) -> None:
        # straight → curve is compatible; surface differs → +0.3 only.
        prev = _ShapeSurface(shape_class="straight", surface_hint="road_tech")
        cand = _ShapeSurface(shape_class="curve", surface_hint="dirt")
        # baseline 0.4 + shape 0.3 = 0.7
        assert _sequence_pair_score(prev, cand) == pytest.approx(0.7)

    def test_incompatible_shape_pair(self) -> None:
        # loop → platform isn't in the compatibility set → baseline only.
        prev = _ShapeSurface(shape_class="loop", surface_hint="plastic")
        cand = _ShapeSurface(shape_class="platform", surface_hint="plastic")
        # baseline 0.4 + surface 0.3 = 0.7 (no shape bonus)
        assert _sequence_pair_score(prev, cand) == pytest.approx(0.7)

    def test_unknown_shape_penalty(self) -> None:
        prev = _ShapeSurface(shape_class="unknown", surface_hint="")
        cand = _ShapeSurface(shape_class="straight", surface_hint="")
        # 0.4 - 0.1 = 0.3
        assert _sequence_pair_score(prev, cand) == pytest.approx(0.3)

    def test_prev_none_is_zero(self) -> None:
        cand = _ShapeSurface(shape_class="straight", surface_hint="road")
        assert _sequence_pair_score(None, cand) == 0.0

    def test_score_bounded_zero_to_one(self) -> None:
        prev = _ShapeSurface(shape_class="start", surface_hint="x")
        cand = _ShapeSurface(shape_class="straight", surface_hint="x")
        score = _sequence_pair_score(prev, cand)
        assert 0.0 <= score <= 1.0


class TestScoreSequenceIntegration:
    def test_sequence_tier_fires_with_lookup(self) -> None:
        cand = _CandidateBlock(
            family="Road", name="RoadTechStraight",
            cell=(1, 9, 0), rotation=0,
            info=GeometryInfo(shape_class="straight"),
        )
        lookup = {
            ("Road", "RoadTechRamp"): _ShapeSurface(
                shape_class="ramp", surface_hint="road_tech",
            ),
            ("Road", "RoadTechStraight"): _ShapeSurface(
                shape_class="straight", surface_hint="road_tech",
            ),
        }
        _, breakdown = score_candidate(
            cand=cand,
            prev_block=("Road", "RoadTechRamp"),
            pair_priors={}, triple_priors=None,
            shape_surface_lookup=lookup,
            path_so_far=[], weights=AI_GENERATOR_WEIGHTS,
        )
        # ramp → straight compatible + same surface → 1.0
        assert breakdown["sequence"] == pytest.approx(1.0)

    def test_sequence_tier_zero_without_lookup(self) -> None:
        # Backcompat: pre-v0.3 callers that don't pass the lookup
        # see the sequence tier as 0 (same as v0.0–v0.2).
        cand = _CandidateBlock(
            family="Road", name="RoadTechStraight",
            cell=(1, 9, 0), rotation=0,
            info=GeometryInfo(shape_class="straight"),
        )
        _, breakdown = score_candidate(
            cand=cand,
            prev_block=("Road", "RoadTechRamp"),
            pair_priors={}, triple_priors=None,
            shape_surface_lookup=None,
            path_so_far=[], weights=AI_GENERATOR_WEIGHTS,
        )
        assert breakdown["sequence"] == 0.0


class TestBeamSearch:
    """v0.2 beam search — width=1 must match greedy; width>1 must
    prune globally; deadends must fall out of the pool."""

    def _cat(self) -> list[_CatalogueEntry]:
        return [
            _CatalogueEntry(
                family="Road", name="RoadTechStraight",
                info=GeometryInfo(shape_class="straight"),
            ),
            _CatalogueEntry(
                family="Road", name="RoadTechCurve",
                info=GeometryInfo(shape_class="curve"),
            ),
            _CatalogueEntry(
                family="Road", name="RoadTechBend",
                info=GeometryInfo(shape_class="curve"),
            ),
        ]

    def test_width_1_matches_greedy(self) -> None:
        # With beam_width=1, same deterministic path as the v0.1
        # greedy loop produced. Priors tie → first catalogue entry
        # wins (stable sort).
        result = _generate_interval(
            src_cell=(0, 9, 0), dst_cell=(4, 9, 0),
            src_block=None,
            catalogue=self._cat(),
            pair_priors={}, triple_priors=None,
            occupied_cells={(0, 9, 0), (4, 9, 0)},
            max_depth=12, weights=AI_GENERATOR_WEIGHTS,
            beam_width=1,
        )
        assert result.reject_reason is None
        assert result.blocks
        # Walker lands within cheb=1 of dst.
        last = result.path_cells[-1]
        assert max(abs(last[0] - 4), abs(last[2])) <= 1

    def test_wider_beam_reaches_same_dst(self) -> None:
        # Width=3 should also reach, with equal-or-better
        # score_sum (wider search never picks worse under greedy
        # tie-break semantics).
        narrow = _generate_interval(
            src_cell=(0, 9, 0), dst_cell=(4, 9, 0),
            src_block=None,
            catalogue=self._cat(),
            pair_priors={}, triple_priors=None,
            occupied_cells={(0, 9, 0), (4, 9, 0)},
            max_depth=12, weights=AI_GENERATOR_WEIGHTS,
            beam_width=1,
        )
        wide = _generate_interval(
            src_cell=(0, 9, 0), dst_cell=(4, 9, 0),
            src_block=None,
            catalogue=self._cat(),
            pair_priors={}, triple_priors=None,
            occupied_cells={(0, 9, 0), (4, 9, 0)},
            max_depth=12, weights=AI_GENERATOR_WEIGHTS,
            beam_width=3,
        )
        assert narrow.reject_reason is None
        assert wide.reject_reason is None
        # Wider beam's mean-score shouldn't be lower on the same
        # problem — if it were, pruning is broken.
        narrow_mean = narrow.score_sum / max(1, narrow.score_count)
        wide_mean = wide.score_sum / max(1, wide.score_count)
        assert wide_mean >= narrow_mean - 1e-9

    def test_beam_carries_diversity_per_path(self) -> None:
        # Two beams exploring different first-step picks should
        # carry distinct prev_block lineage (the diversity penalty
        # and pair/triple priors reference per-beam path, not a
        # shared mutable).
        # We assert indirectly: the winning beam's block list is
        # a subset of its own ancestor — not a concatenation across
        # beams. Easiest check: all path_cells are contiguous.
        result = _generate_interval(
            src_cell=(0, 9, 0), dst_cell=(5, 9, 0),
            src_block=None,
            catalogue=self._cat(),
            pair_priors={}, triple_priors=None,
            occupied_cells={(0, 9, 0), (5, 9, 0)},
            max_depth=12, weights=AI_GENERATOR_WEIGHTS,
            beam_width=3,
        )
        assert result.reject_reason is None
        for i in range(1, len(result.path_cells)):
            a, b = result.path_cells[i - 1], result.path_cells[i]
            assert max(abs(a[0] - b[0]), abs(a[2] - b[2])) <= 1

    def test_occupancy_across_siblings_isolated(self) -> None:
        # Caller's occupied set stays read-only during a beam
        # search; a beam's placements belong to ITS frozen
        # interval_cells only. No sibling can collide with another's
        # choice because rotation + direction are shared up to the
        # split point, but the assertion here is lighter: the
        # initial occupancy set (anchors) must NOT grow during
        # expansion of competing beams.
        occ = {(0, 9, 0), (5, 9, 0)}
        snapshot = set(occ)
        _generate_interval(
            src_cell=(0, 9, 0), dst_cell=(5, 9, 0),
            src_block=None,
            catalogue=self._cat(),
            pair_priors={}, triple_priors=None,
            occupied_cells=occ,
            max_depth=12, weights=AI_GENERATOR_WEIGHTS,
            beam_width=3,
        )
        # Anchor cells are still there; additional cells (winning
        # beam's interval) got merged back in — that's the documented
        # protocol so downstream intervals don't collide.
        assert snapshot.issubset(occ)
        # Net-new cells = winning beam's path, which is >= 1 cell
        # (non-trivial interval).
        assert len(occ) > len(snapshot)

    def test_partial_beam_preserved_on_exhaustion(self) -> None:
        # max_depth too small to reach dst → reject_reason=beam_exhausted,
        # but result.blocks keeps the best partial beam so the
        # operator can inspect ai_score_breakdown post-hoc.
        result = _generate_interval(
            src_cell=(0, 9, 0), dst_cell=(100, 9, 0),
            src_block=None,
            catalogue=self._cat(),
            pair_priors={}, triple_priors=None,
            occupied_cells={(0, 9, 0), (100, 9, 0)},
            max_depth=4, weights=AI_GENERATOR_WEIGHTS,
            beam_width=2,
        )
        assert result.reject_reason == "beam_exhausted"
        assert len(result.blocks) == 4


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


