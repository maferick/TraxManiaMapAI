from __future__ import annotations

import pytest

from src.constraints.evidence import (
    SUSPICIOUS,
    UNKNOWN,
    VALID,
    derive_validity_label,
)


def _label(**kwargs) -> str:
    defaults = dict(
        benchmark_strong_count=0,
        broken_fixture_count=0,
        replay_supported_count=0,
        observed_in_maps_count=0,
    )
    defaults.update(kwargs)
    return derive_validity_label(**defaults)


def test_benchmark_strong_upgrades_to_valid() -> None:
    assert _label(benchmark_strong_count=1) == VALID


def test_benchmark_strong_wins_over_broken() -> None:
    # A benchmark-strong occurrence wins even if the edge also shows up
    # in a broken fixture — an edge that appears in a known-good map is
    # by definition not structurally invalid.
    assert _label(benchmark_strong_count=1, broken_fixture_count=3) == VALID


def test_broken_only_is_suspicious() -> None:
    assert _label(broken_fixture_count=1) == SUSPICIOUS


def test_no_evidence_is_unknown() -> None:
    assert _label() == UNKNOWN


def test_high_frequency_without_evidence_stays_unknown() -> None:
    # The load-bearing invariant: frequency is not validity.
    assert _label(observed_in_maps_count=10_000) == UNKNOWN


def test_replay_support_alone_does_not_upgrade() -> None:
    # Replay support currently cannot upgrade to valid on its own —
    # replay-to-block projection isn't wired to cohort filtering yet.
    assert _label(replay_supported_count=5) == UNKNOWN


def test_replay_support_does_not_trigger_suspicious() -> None:
    # If replay_supported_count > 0, we no longer treat a broken-fixture
    # occurrence as suspicious (there's some observed use). It degrades
    # to unknown (pending a stricter policy once replay support tightens).
    assert _label(broken_fixture_count=1, replay_supported_count=1) == UNKNOWN


def test_negative_counts_rejected() -> None:
    with pytest.raises(ValueError, match="benchmark_strong_count"):
        _label(benchmark_strong_count=-1)
    with pytest.raises(ValueError, match="broken_fixture_count"):
        _label(broken_fixture_count=-3)
    with pytest.raises(ValueError, match="replay_supported_count"):
        _label(replay_supported_count=-2)
