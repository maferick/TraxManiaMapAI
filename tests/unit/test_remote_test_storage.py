"""Tests for :mod:`src.remote_test.storage`.

Focus: state-transition invariants, hash-based dedup, agent
heartbeat upsert. The Flask app test covers the HTTP surface
separately.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.remote_test.models import JobStatus, is_terminal
from src.remote_test.storage import JobStore, JobStoreError


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.db")


@pytest.fixture
def art_root(tmp_path: Path) -> Path:
    p = tmp_path / "artifacts"
    p.mkdir()
    return p


class TestEnqueue:
    def test_enqueue_creates_queued_job(
        self, store: JobStore, art_root: Path,
    ) -> None:
        r = store.enqueue(
            run_id="abc123",
            artifact_bytes=b"MAP_BYTES",
            artifacts_root=art_root,
            metadata={"base_map_id": 1212},
        )
        job = store.get_strict(r.job_id)
        assert job.status is JobStatus.QUEUED
        assert job.run_id == "abc123"
        assert job.artifact_size == len(b"MAP_BYTES")
        assert job.artifact_sha256 == r.artifact_sha256
        assert job.metadata == {"base_map_id": 1212}

    def test_enqueue_dedupes_identical_artifacts(
        self, store: JobStore, art_root: Path,
    ) -> None:
        r1 = store.enqueue(
            run_id="A", artifact_bytes=b"X",
            artifacts_root=art_root,
        )
        r2 = store.enqueue(
            run_id="B", artifact_bytes=b"X",
            artifacts_root=art_root,
        )
        assert r1.artifact_sha256 == r2.artifact_sha256
        # Two separate jobs, but only one file on disk.
        files = list(art_root.glob("*.Map.Gbx"))
        assert len(files) == 1

    def test_enqueue_rejects_empty_run_id(
        self, store: JobStore, art_root: Path,
    ) -> None:
        with pytest.raises(JobStoreError, match="run_id required"):
            store.enqueue(
                run_id="", artifact_bytes=b"X", artifacts_root=art_root,
            )

    def test_enqueue_rejects_nonpositive_timeout(
        self, store: JobStore, art_root: Path,
    ) -> None:
        with pytest.raises(JobStoreError, match="timeout"):
            store.enqueue(
                run_id="A", artifact_bytes=b"X",
                artifacts_root=art_root, timeout_seconds=0,
            )


class TestClaimNext:
    def test_claims_oldest_queued(
        self, store: JobStore, art_root: Path,
    ) -> None:
        r1 = store.enqueue(
            run_id="first", artifact_bytes=b"1",
            artifacts_root=art_root,
        )
        # Bump second's created_at by sleeping — SQLite's CURRENT_TIMESTAMP
        # has second resolution so an explicit delay keeps ordering
        # deterministic in the test.
        time.sleep(1)
        store.enqueue(
            run_id="second", artifact_bytes=b"2",
            artifacts_root=art_root,
        )
        claimed = store.claim_next(agent_id="agent-1")
        assert claimed is not None
        assert claimed.id == r1.job_id
        assert claimed.status is JobStatus.CLAIMED
        assert claimed.agent_id == "agent-1"
        assert claimed.claimed_at is not None

    def test_claim_returns_none_when_empty(self, store: JobStore) -> None:
        assert store.claim_next(agent_id="agent-1") is None

    def test_claim_requires_agent_id(self, store: JobStore) -> None:
        with pytest.raises(JobStoreError, match="agent_id required"):
            store.claim_next(agent_id="")


class TestTransitions:
    def _queued_job(
        self, store: JobStore, art_root: Path,
    ) -> int:
        r = store.enqueue(
            run_id="r", artifact_bytes=b"X", artifacts_root=art_root,
        )
        return r.job_id

    def test_queued_to_claimed_to_running_to_complete(
        self, store: JobStore, art_root: Path,
    ) -> None:
        jid = self._queued_job(store, art_root)
        claimed = store.claim_next(agent_id="a")
        assert claimed is not None
        store.transition(
            job_id=jid, to_status=JobStatus.RUNNING, agent_id="a",
            detail="map loading",
        )
        job = store.transition(
            job_id=jid, to_status=JobStatus.COMPLETE,
            agent_id="a",
            report={"driven_cells": 42, "finished": True},
        )
        assert job.status is JobStatus.COMPLETE
        assert job.report == {"driven_cells": 42, "finished": True}
        assert job.completed_at is not None
        assert is_terminal(job.status)

    def test_rejects_invalid_transition(
        self, store: JobStore, art_root: Path,
    ) -> None:
        jid = self._queued_job(store, art_root)
        # queued → complete isn't allowed (must claim first)
        with pytest.raises(JobStoreError, match="cannot transition"):
            store.transition(
                job_id=jid, to_status=JobStatus.COMPLETE,
            )

    def test_rejects_different_agent(
        self, store: JobStore, art_root: Path,
    ) -> None:
        jid = self._queued_job(store, art_root)
        store.claim_next(agent_id="a")
        with pytest.raises(JobStoreError, match="claimed by"):
            store.transition(
                job_id=jid, to_status=JobStatus.RUNNING, agent_id="b",
            )

    def test_terminal_status_rejects_further_moves(
        self, store: JobStore, art_root: Path,
    ) -> None:
        jid = self._queued_job(store, art_root)
        store.claim_next(agent_id="a")
        store.transition(
            job_id=jid, to_status=JobStatus.COMPLETE, agent_id="a",
        )
        # Can't move a terminal job.
        with pytest.raises(JobStoreError):
            store.transition(
                job_id=jid, to_status=JobStatus.RUNNING, agent_id="a",
            )


class TestSweepTimeouts:
    def test_sweeps_claimed_past_deadline(
        self, store: JobStore, art_root: Path,
    ) -> None:
        store.enqueue(
            run_id="r", artifact_bytes=b"X",
            artifacts_root=art_root, timeout_seconds=1,
        )
        store.claim_next(agent_id="a")
        # Fast-forward: simulate deadline by patching the row's
        # claimed_at in the raw DB.
        with store._lock:  # type: ignore[attr-defined]
            store._conn.execute(  # type: ignore[attr-defined]
                "UPDATE jobs SET claimed_at = claimed_at - 10",
            )
            store._conn.commit()  # type: ignore[attr-defined]
        affected = store.sweep_timeouts()
        assert len(affected) == 1
        j = store.get_strict(affected[0])
        assert j.status is JobStatus.TIMED_OUT
        assert j.completed_at is not None

    def test_leaves_unclaimed_queued_alone(
        self, store: JobStore, art_root: Path,
    ) -> None:
        r = store.enqueue(
            run_id="r", artifact_bytes=b"X",
            artifacts_root=art_root, timeout_seconds=1,
        )
        affected = store.sweep_timeouts()
        assert affected == []
        assert store.get_strict(r.job_id).status is JobStatus.QUEUED


class TestHeartbeats:
    def test_upsert_inserts_then_updates(self, store: JobStore) -> None:
        hb1 = store.upsert_agent_heartbeat(
            agent_id="win-01", version="0.1", hostname="gamer-pc",
        )
        hb2 = store.upsert_agent_heartbeat(
            agent_id="win-01", version="0.2",
        )
        assert hb1.agent_id == hb2.agent_id == "win-01"
        # hostname preserved across the update even though the
        # second call didn't include it.
        listed = store.list_agents()
        assert len(listed) == 1
        assert listed[0].hostname == "gamer-pc"
        assert listed[0].version == "0.2"
