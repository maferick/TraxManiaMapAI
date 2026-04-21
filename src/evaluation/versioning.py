"""Semver parsing and comparison for evaluator and surrogate versions.

The three-level semantics (major / minor / patch) are defined in
``docs/architecture.md`` and are load-bearing: drift monitoring needs
to distinguish a score-incompatible bump from a no-op fix.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

_SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class VersionCompatibility(Enum):
    """How a stored artifact relates to the currently-running version."""

    SAME = "same"
    PATCH_DIFFERENT = "patch_different"
    MINOR_DIFFERENT = "minor_different"
    MAJOR_DIFFERENT = "major_different"


@dataclass(frozen=True, order=True)
class EvaluatorVersion:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> EvaluatorVersion:
        if not isinstance(value, str):
            raise TypeError(f"version must be a string, got {type(value).__name__}")
        match = _SEMVER_RE.match(value)
        if match is None:
            raise ValueError(
                f"invalid evaluator version {value!r}: expected MAJOR.MINOR.PATCH "
                "with non-negative integers and no leading zeros"
            )
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def compare(self, other: EvaluatorVersion) -> VersionCompatibility:
        if self.major != other.major:
            return VersionCompatibility.MAJOR_DIFFERENT
        if self.minor != other.minor:
            return VersionCompatibility.MINOR_DIFFERENT
        if self.patch != other.patch:
            return VersionCompatibility.PATCH_DIFFERENT
        return VersionCompatibility.SAME


def invalidates_rankings(current: str, stored: str) -> bool:
    """True iff an artifact produced at ``stored`` must be re-scored before
    being compared against ``current``.

    Only a major-version difference invalidates rankings. A minor bump is
    additive and a patch is a no-op (see ``docs/architecture.md``).
    """
    return EvaluatorVersion.parse(current).major != EvaluatorVersion.parse(stored).major
