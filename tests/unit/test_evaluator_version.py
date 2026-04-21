from __future__ import annotations

import pytest

from src.evaluation.versioning import (
    EvaluatorVersion,
    VersionCompatibility,
    invalidates_rankings,
)


class TestEvaluatorVersionParse:
    def test_parses_standard_semver(self) -> None:
        v = EvaluatorVersion.parse("1.2.3")
        assert v == EvaluatorVersion(1, 2, 3)

    def test_parses_zero_major(self) -> None:
        assert EvaluatorVersion.parse("0.1.0") == EvaluatorVersion(0, 1, 0)

    def test_round_trips_to_string(self) -> None:
        assert str(EvaluatorVersion.parse("4.5.6")) == "4.5.6"

    @pytest.mark.parametrize(
        "bad",
        [
            "1.2",
            "v1.2.3",
            "1.2.3.4",
            "1.2.3-rc1",
            "01.2.3",
            "1.02.3",
            "1.2.03",
            "-1.2.3",
            "1.2.3 ",
            " 1.2.3",
            "",
            "latest",
        ],
    )
    def test_rejects_malformed(self, bad: str) -> None:
        with pytest.raises(ValueError):
            EvaluatorVersion.parse(bad)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(TypeError):
            EvaluatorVersion.parse(123)  # type: ignore[arg-type]


class TestEvaluatorVersionOrdering:
    def test_ordering_follows_tuple(self) -> None:
        assert EvaluatorVersion(1, 0, 0) < EvaluatorVersion(1, 0, 1)
        assert EvaluatorVersion(1, 1, 0) > EvaluatorVersion(1, 0, 99)
        assert EvaluatorVersion(2, 0, 0) > EvaluatorVersion(1, 99, 99)


class TestCompare:
    def test_same(self) -> None:
        assert EvaluatorVersion(1, 2, 3).compare(EvaluatorVersion(1, 2, 3)) == VersionCompatibility.SAME

    def test_patch_different(self) -> None:
        assert (
            EvaluatorVersion(1, 2, 3).compare(EvaluatorVersion(1, 2, 4))
            == VersionCompatibility.PATCH_DIFFERENT
        )

    def test_minor_different(self) -> None:
        assert (
            EvaluatorVersion(1, 2, 3).compare(EvaluatorVersion(1, 3, 0))
            == VersionCompatibility.MINOR_DIFFERENT
        )

    def test_major_different_dominates(self) -> None:
        assert (
            EvaluatorVersion(1, 2, 3).compare(EvaluatorVersion(2, 2, 3))
            == VersionCompatibility.MAJOR_DIFFERENT
        )
        assert (
            EvaluatorVersion(1, 9, 9).compare(EvaluatorVersion(2, 0, 0))
            == VersionCompatibility.MAJOR_DIFFERENT
        )


class TestInvalidatesRankings:
    def test_major_bump_invalidates(self) -> None:
        assert invalidates_rankings("2.0.0", "1.9.9") is True

    def test_minor_bump_does_not_invalidate(self) -> None:
        assert invalidates_rankings("1.2.0", "1.1.0") is False

    def test_patch_bump_does_not_invalidate(self) -> None:
        assert invalidates_rankings("1.0.1", "1.0.0") is False

    def test_same_version(self) -> None:
        assert invalidates_rankings("1.0.0", "1.0.0") is False
