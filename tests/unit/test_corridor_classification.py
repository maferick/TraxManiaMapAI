"""Tests for traversability family classification (Phase 1 artifact)."""
from __future__ import annotations

import pytest

from src.corridor.traversability import (
    AMBIGUOUS_FAMILIES,
    DRIVABLE_FAMILIES,
    FamilyBucket,
    NON_DRIVABLE_FAMILIES,
    classify_family,
)
from src.corridor.traversability.classification import CLASSIFICATION_VERSION


class TestBucketIntegrity:
    """The three buckets must be disjoint and non-empty. Module import
    already validates disjointedness via ``_validate_buckets``; these
    tests assert the same invariant explicitly so a future editor sees
    a failing test, not an import-time exception inside a CI run."""

    def test_all_buckets_are_nonempty(self) -> None:
        assert len(DRIVABLE_FAMILIES) > 0
        assert len(NON_DRIVABLE_FAMILIES) > 0
        assert len(AMBIGUOUS_FAMILIES) > 0

    def test_buckets_are_pairwise_disjoint(self) -> None:
        assert DRIVABLE_FAMILIES.isdisjoint(NON_DRIVABLE_FAMILIES)
        assert DRIVABLE_FAMILIES.isdisjoint(AMBIGUOUS_FAMILIES)
        assert NON_DRIVABLE_FAMILIES.isdisjoint(AMBIGUOUS_FAMILIES)

    def test_classification_version_is_nonempty_string(self) -> None:
        # Version is downstream-load-bearing: evidence rows will carry it.
        assert isinstance(CLASSIFICATION_VERSION, str)
        assert CLASSIFICATION_VERSION.count(".") == 2  # semver-lite


class TestCoreDrivableFamilies:
    """These must stay drivable — they're the track-family backbone."""

    @pytest.mark.parametrize("family", ["Platform", "Road", "Track", "Gate"])
    def test_primary_track_families_are_drivable(self, family: str) -> None:
        assert classify_family(family) is FamilyBucket.DRIVABLE

    @pytest.mark.parametrize("family", ["Technics", "Rally", "Snow", "Dirt"])
    def test_secondary_track_families_are_drivable(self, family: str) -> None:
        assert classify_family(family) is FamilyBucket.DRIVABLE


class TestCoreNonDrivableFamilies:
    """These must stay non-drivable — they're the noise-source families
    the traversability pruning MUST suppress to hit the ≥80% deco-removal
    commit bar."""

    def test_deco_is_non_drivable(self) -> None:
        # 2.24M placements in the scale-1k audit; single biggest noise.
        assert classify_family("Deco") is FamilyBucket.NON_DRIVABLE

    def test_structure_is_non_drivable(self) -> None:
        # Track supports / stadium structural supports.
        assert classify_family("Structure") is FamilyBucket.NON_DRIVABLE

    @pytest.mark.parametrize("family", ["Stand", "Canopy", "Stadium"])
    def test_stadium_scenery_is_non_drivable(self, family: str) -> None:
        assert classify_family(family) is FamilyBucket.NON_DRIVABLE

    @pytest.mark.parametrize("family", ["Water", "Void", "Lake", "Ground", "Grass", "Land"])
    def test_environmental_families_are_non_drivable(self, family: str) -> None:
        assert classify_family(family) is FamilyBucket.NON_DRIVABLE


class TestUnknownIsBlocked:
    """The specifically-named ``Unknown`` family (user-imported custom
    blocks) is explicitly ``NON_DRIVABLE`` — fail-safe default. This is
    distinct from unseen-family default, which is ``AMBIGUOUS``."""

    def test_unknown_family_defaults_to_blocked(self) -> None:
        assert classify_family("Unknown") is FamilyBucket.NON_DRIVABLE


class TestAmbiguousFamilies:
    """Families that need per-block-type review stay ``AMBIGUOUS`` until
    Phase 3 evidence logic runs. Classifying them at the family level
    would either admit deco into corridors (if DRIVABLE) or reject
    legitimate drivable blocks (if NON_DRIVABLE)."""

    @pytest.mark.parametrize("family", ["Open", "Stage", "Items", "Nations"])
    def test_mixed_families_are_ambiguous(self, family: str) -> None:
        assert classify_family(family) is FamilyBucket.AMBIGUOUS

    @pytest.mark.parametrize("family", ["Trackmania", "Tm", "Block", "Wood", "Plastic"])
    def test_heuristic_misfire_candidates_are_ambiguous(self, family: str) -> None:
        assert classify_family(family) is FamilyBucket.AMBIGUOUS


class TestUnseenFamilyDefault:
    """Brand-new families (not yet in any bucket) must surface as
    review-required (``AMBIGUOUS``), not silently drop into a default
    that could be wrong for whole categories of new blocks Nadeo ships."""

    def test_unseen_family_returns_ambiguous(self) -> None:
        assert classify_family("SomeFutureNadeoFamily") is FamilyBucket.AMBIGUOUS

    def test_empty_string_returns_ambiguous(self) -> None:
        # Defensive: the block_family column is NOT NULL but could be
        # empty on a pathological block_type. Fail safe to AMBIGUOUS.
        assert classify_family("") is FamilyBucket.AMBIGUOUS


class TestEnumSemantics:
    """FamilyBucket values are load-bearing for persistence —
    downstream evidence tables will store them verbatim."""

    def test_bucket_values_are_stable_strings(self) -> None:
        assert FamilyBucket.DRIVABLE.value == "drivable"
        assert FamilyBucket.NON_DRIVABLE.value == "non_drivable"
        assert FamilyBucket.AMBIGUOUS.value == "ambiguous"

    def test_bucket_is_str_enum(self) -> None:
        # Subclass of str so a FamilyBucket instance can go straight into
        # pymysql execute() without conversion. Catches accidental refactors.
        assert isinstance(FamilyBucket.DRIVABLE, str)
