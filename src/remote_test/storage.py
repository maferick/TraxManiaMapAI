"""SQLite-backed job store for the remote-test queue.

Schema lives inline so the store self-migrates on open — this is a
single-process service, no multi-writer drama, and we never expect
to want the server live while a migration runs.

Thread-safety: ``sqlite3.connect`` with ``check_same_thread=False``
plus a module-level lock on writes. Flask's default dev server is
single-threaded, but production deployments run behind gunicorn
with workers > 1; the lock protects the common case.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from src.remote_test.models import (
    AgentHeartbeat,
    Job,
    JobStatus,
    _json_or_empty,
    _json_or_none,
    is_terminal,
)

_LOG = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT    NOT NULL,
    status            TEXT    NOT NULL,
    artifact_path     TEXT    NOT NULL,
    artifact_size     INTEGER NOT NULL,
    artifact_sha256   TEXT    NOT NULL,
    timeout_seconds   INTEGER NOT NULL,
    created_at        INTEGER NOT NULL,
    claimed_at        INTEGER,
    completed_at      INTEGER,
    agent_id          TEXT,
    metadata_json     TEXT,
    report_json       TEXT,
    detail            TEXT
);

CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS ix_jobs_created ON jobs(created_at);

CREATE TABLE IF NOT EXISTS agent_heartbeats (
    agent_id     TEXT PRIMARY KEY,
    last_seen_at INTEGER NOT NULL,
    version      TEXT,
    hostname     TEXT,
    notes        TEXT
);
"""


_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({
        JobStatus.CLAIMED, JobStatus.CANCELLED, JobStatus.TIMED_OUT,
    }),
    JobStatus.CLAIMED: frozenset({
        JobStatus.RUNNING, JobStatus.COMPLETE, JobStatus.FAILED,
        JobStatus.TIMED_OUT, JobStatus.CANCELLED,
    }),
    JobStatus.RUNNING: frozenset({
        JobStatus.COMPLETE, JobStatus.FAILED,
        JobStatus.TIMED_OUT, JobStatus.CANCELLED,
    }),
    # Terminals: no outgoing transitions.
}


class JobStoreError(Exception):
    """Raised on any invariant violation the store can detect."""


def _row_to_job(r: sqlite3.Row) -> Job:
    return Job(
        id=int(r["id"]),
        run_id=str(r["run_id"]),
        status=JobStatus(str(r["status"])),
        artifact_path=str(r["artifact_path"]),
        artifact_size=int(r["artifact_size"]),
        artifact_sha256=str(r["artifact_sha256"]),
        timeout_seconds=int(r["timeout_seconds"]),
        created_at=int(r["created_at"]),
        claimed_at=int(r["claimed_at"]) if r["claimed_at"] is not None else None,
        completed_at=(
            int(r["completed_at"]) if r["completed_at"] is not None else None
        ),
        agent_id=str(r["agent_id"]) if r["agent_id"] is not None else None,
        metadata=_json_or_empty(r["metadata_json"]),
        report=_json_or_none(r["report_json"]),
        detail=str(r["detail"]) if r["detail"] is not None else None,
    )


@dataclass
class EnqueueResult:
    """Returned by :meth:`JobStore.enqueue` — lets the CLI side
    surface the computed artifact hash without reopening the job."""
    job_id: int
    artifact_sha256: str
    artifact_size: int


class JobStore:
    """Single-file SQLite store. ``path`` is the DB file; the
    artifact directory is managed separately (server.py knows the
    root). Keep the DB <-> artifact split so the store stays
    tight + the server can serve artifacts without going through
    SQLAlchemy-style ORM ceremony.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------

    def enqueue(
        self,
        *,
        run_id: str,
        artifact_bytes: bytes,
        artifacts_root: Path,
        metadata: dict[str, Any] | None = None,
        timeout_seconds: int = 300,
    ) -> EnqueueResult:
        """Persist a new job + its artifact. Hash the artifact on
        disk (SHA-256, used later by the agent to validate the
        download); raise :class:`JobStoreError` if the resulting
        file already exists with a different hash — prevents
        different-but-same-run_id collisions from silently
        overwriting prior artifacts."""
        if not isinstance(artifact_bytes, (bytes, bytearray)):
            raise JobStoreError("artifact_bytes must be bytes-like")
        if timeout_seconds <= 0:
            raise JobStoreError("timeout_seconds must be positive")
        if not run_id:
            raise JobStoreError("run_id required")

        digest = hashlib.sha256(artifact_bytes).hexdigest()
        size = len(artifact_bytes)

        artifacts_root.mkdir(parents=True, exist_ok=True)
        # Store by sha256 so identical artifacts are deduplicated
        # on disk — two enqueues of the same GBX both reference the
        # same bytes.
        target = artifacts_root / f"{digest}.Map.Gbx"
        if target.exists():
            existing = hashlib.sha256(target.read_bytes()).hexdigest()
            if existing != digest:
                raise JobStoreError(
                    f"artifact path {target} has a different hash "
                    f"({existing}) than computed ({digest})"
                )
        else:
            target.write_bytes(artifact_bytes)

        created_at = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs (run_id, status, artifact_path, "
                "  artifact_size, artifact_sha256, timeout_seconds, "
                "  created_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, JobStatus.QUEUED.value,
                    str(target.name), size, digest,
                    int(timeout_seconds), created_at,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
            self._conn.commit()
            job_id = int(cur.lastrowid)
        _LOG.info(
            "enqueued job %d run_id=%s sha256=%s size=%d",
            job_id, run_id, digest[:12] + "…", size,
        )
        return EnqueueResult(
            job_id=job_id, artifact_sha256=digest, artifact_size=size,
        )

    def claim_next(self, *, agent_id: str) -> Job | None:
        """Atomically pick the oldest queued job + mark it CLAIMED
        by the given agent. Returns ``None`` when the queue is
        empty (HTTP-level: ``204 No Content``).
        """
        if not agent_id:
            raise JobStoreError("agent_id required")
        now = int(time.time())
        with self._lock:
            # Single-row transaction: select-for-update via
            # UPDATE...WHERE rowid IN (...) pattern. SQLite doesn't
            # do SELECT FOR UPDATE, but our lock serialises writers
            # so this is safe.
            row = self._conn.execute(
                "SELECT id FROM jobs WHERE status = ? "
                "ORDER BY created_at ASC, id ASC LIMIT 1",
                (JobStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            job_id = int(row["id"])
            self._conn.execute(
                "UPDATE jobs SET status = ?, agent_id = ?, claimed_at = ? "
                "WHERE id = ? AND status = ?",
                (
                    JobStatus.CLAIMED.value, agent_id, now,
                    job_id, JobStatus.QUEUED.value,
                ),
            )
            self._conn.commit()
        return self.get(job_id)

    def transition(
        self,
        *,
        job_id: int,
        to_status: JobStatus,
        agent_id: str | None = None,
        detail: str | None = None,
        report: dict[str, Any] | None = None,
    ) -> Job:
        """Apply a state transition. Enforces the
        :data:`_ALLOWED_TRANSITIONS` graph; raises on invalid
        moves so the agent can't silently get jobs into impossible
        states. Terminals also set ``completed_at``."""
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,),
            ).fetchone()
            if row is None:
                raise JobStoreError(f"job {job_id} not found")
            current = JobStatus(str(row["status"]))
            allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
            if to_status not in allowed:
                raise JobStoreError(
                    f"cannot transition job {job_id} from {current.value} "
                    f"to {to_status.value}; allowed: "
                    f"{sorted(s.value for s in allowed)}"
                )
            if agent_id is not None and row["agent_id"] not in (None, agent_id):
                raise JobStoreError(
                    f"job {job_id} is claimed by {row['agent_id']!r}, "
                    f"not {agent_id!r}"
                )
            completed_at = now if is_terminal(to_status) else None
            self._conn.execute(
                "UPDATE jobs SET status = ?, detail = COALESCE(?, detail), "
                "  report_json = COALESCE(?, report_json), "
                "  completed_at = COALESCE(?, completed_at) "
                "WHERE id = ?",
                (
                    to_status.value, detail,
                    json.dumps(report, sort_keys=True) if report else None,
                    completed_at,
                    job_id,
                ),
            )
            self._conn.commit()
        return self.get_strict(job_id)

    def upsert_agent_heartbeat(
        self,
        *,
        agent_id: str,
        version: str | None = None,
        hostname: str | None = None,
        notes: str | None = None,
    ) -> AgentHeartbeat:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_heartbeats "
                "(agent_id, last_seen_at, version, hostname, notes) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(agent_id) DO UPDATE SET "
                "  last_seen_at = excluded.last_seen_at, "
                "  version = COALESCE(excluded.version, version), "
                "  hostname = COALESCE(excluded.hostname, hostname), "
                "  notes = COALESCE(excluded.notes, notes)",
                (agent_id, now, version, hostname, notes),
            )
            self._conn.commit()
        return AgentHeartbeat(
            agent_id=agent_id, last_seen_at=now,
            version=version, hostname=hostname, notes=notes,
        )

    def sweep_timeouts(self) -> list[int]:
        """Background housekeeping — jobs in CLAIMED/RUNNING past
        their ``timeout_seconds`` since claim get moved to
        TIMED_OUT. Returns the affected job IDs.

        Called by the server on every ``GET /jobs/next`` so callers
        never see stale "claimed" states from agents that went
        offline without reporting.
        """
        now = int(time.time())
        affected: list[int] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, timeout_seconds, claimed_at FROM jobs "
                "WHERE status IN (?, ?) "
                "  AND claimed_at IS NOT NULL "
                "  AND (? - claimed_at) > timeout_seconds",
                (JobStatus.CLAIMED.value, JobStatus.RUNNING.value, now),
            ).fetchall()
            for r in rows:
                jid = int(r["id"])
                self._conn.execute(
                    "UPDATE jobs SET status = ?, completed_at = ?, "
                    "  detail = COALESCE(detail, '') || ' | swept by server' "
                    "WHERE id = ?",
                    (JobStatus.TIMED_OUT.value, now, jid),
                )
                affected.append(jid)
            if affected:
                self._conn.commit()
        return affected

    # ------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------

    def get(self, job_id: int) -> Job | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,),
        ).fetchone()
        return _row_to_job(row) if row is not None else None

    def get_strict(self, job_id: int) -> Job:
        j = self.get(job_id)
        if j is None:
            raise JobStoreError(f"job {job_id} not found")
        return j

    def list_recent(self, limit: int = 50) -> list[Job]:
        rows = self._conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    def list_agents(self) -> list[AgentHeartbeat]:
        rows = self._conn.execute(
            "SELECT * FROM agent_heartbeats ORDER BY last_seen_at DESC"
        ).fetchall()
        return [
            AgentHeartbeat(
                agent_id=str(r["agent_id"]),
                last_seen_at=int(r["last_seen_at"]),
                version=str(r["version"]) if r["version"] is not None else None,
                hostname=(
                    str(r["hostname"]) if r["hostname"] is not None else None
                ),
                notes=str(r["notes"]) if r["notes"] is not None else None,
            )
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
