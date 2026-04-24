"""Phase 2 PR M — unit tests for finishability-proof derivation.

The DB upsert + GBX re-parse path is exercised by the live smoke in
the PR body (needs a real connection + built wrapper). These unit
tests cover the pure derivation helpers.
"""
from __future__ import annotations

import pytest

from src.generation.finishability_proof import (
    PROOF_SOURCE_AUTHOR_TIME,
    PROOF_SOURCE_INTERNAL_ROUTE,
    PROOF_SOURCE_NONE,
    PROOF_SOURCE_REPLAY,
    PROOF_SOURCE_WORLD_RECORD,
    derive_proof_source,
)


class TestDeriveProofSource:
    def test_clean_replay_is_strongest(self) -> None:
        # Even with everything else true, a clean replay wins.
        assert derive_proof_source(
            has_author_time=True,
            has_clean_replay=True,
            has_any_replay=True,
            has_internal_route=True,
        ) == PROOF_SOURCE_REPLAY

    def test_author_time_when_no_clean_replay(self) -> None:
        assert derive_proof_source(
            has_author_time=True,
            has_clean_replay=False,
            has_any_replay=True,
            has_internal_route=True,
        ) == PROOF_SOURCE_AUTHOR_TIME

    def test_world_record_when_only_dirty_replays(self) -> None:
        # Replay exists but none marked clean + no author time.
        assert derive_proof_source(
            has_author_time=False,
            has_clean_replay=False,
            has_any_replay=True,
            has_internal_route=True,
        ) == PROOF_SOURCE_WORLD_RECORD

    def test_internal_route_when_only_our_gate(self) -> None:
        assert derive_proof_source(
            has_author_time=False,
            has_clean_replay=False,
            has_any_replay=False,
            has_internal_route=True,
        ) == PROOF_SOURCE_INTERNAL_ROUTE

    def test_none_when_no_evidence(self) -> None:
        assert derive_proof_source(
            has_author_time=False,
            has_clean_replay=False,
            has_any_replay=False,
            has_internal_route=False,
        ) == PROOF_SOURCE_NONE

    def test_author_time_beats_internal_route(self) -> None:
        # Author-declared evidence outranks our own internal gate.
        assert derive_proof_source(
            has_author_time=True,
            has_clean_replay=False,
            has_any_replay=False,
            has_internal_route=True,
        ) == PROOF_SOURCE_AUTHOR_TIME

    def test_world_record_beats_internal_route(self) -> None:
        # A player finishing the map (even un-cleaned) outranks our
        # gate-only signal.
        assert derive_proof_source(
            has_author_time=False,
            has_clean_replay=False,
            has_any_replay=True,
            has_internal_route=True,
        ) == PROOF_SOURCE_WORLD_RECORD
