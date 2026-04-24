"""Data shapes for the remote-test queue.

All dataclasses are intentionally plain (no pydantic etc.) so they
round-trip cleanly through JSON + SQLite without extra deps.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    """Closed enum of job lifecycle states.

    Transitions (enforced by :class:`src.remote_test.storage.JobStore`):

      QUEUED → CLAIMED   (agent picks the job via ``GET /jobs/next``)
      CLAIMED → RUNNING  (agent reports it started running, e.g. TM
                          launched and map is loading)
      RUNNING → COMPLETE (agent posted telemetry report)
      RUNNING → FAILED   (agent reports explicit failure pre-telemetry)
      CLAIMED/RUNNING → TIMED_OUT
                         (job exceeded its timeout before the agent
                          reported anything terminal)
      * → CANCELLED      (operator-initiated kill)

    ``QUEUED`` is the only state where ``GET /jobs/next`` will pick
    a job up. ``COMPLETE`` / ``FAILED`` / ``TIMED_OUT`` / ``CANCELLED``
    are terminal.
    """
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


_TERMINAL = frozenset({
    JobStatus.COMPLETE, JobStatus.FAILED,
    JobStatus.TIMED_OUT, JobStatus.CANCELLED,
})


def is_terminal(status: JobStatus) -> bool:
    return status in _TERMINAL


@dataclass
class Job:
    """One unit of work the agent pulls, runs, and reports on.

    ``artifact_sha256`` is computed by the enqueuing side and
    shipped to the agent; the agent verifies the download before
    copying the GBX to the TM2020 Maps folder, which catches both
    LAN corruption and server-side artifact swaps.

    ``metadata`` is a free-form dict the CLI side uses to pass
    context the agent/plugin may want to log (base_map_id,
    random_seed, ai_generator_version, run_id). The server treats
    it as an opaque blob — no schema on purpose, so iterating on
    the agent side doesn't require server changes.
    """
    id: int
    run_id: str                       # artifact run_id (16-hex or similar)
    status: JobStatus
    artifact_path: str                # relative path under artifacts_root
    artifact_size: int
    artifact_sha256: str
    timeout_seconds: int
    created_at: int                   # unix epoch seconds
    agent_id: str | None = None
    claimed_at: int | None = None
    completed_at: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] | None = None
    detail: str | None = None         # last status message

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "status": self.status.value,
            "artifact_size": self.artifact_size,
            "artifact_sha256": self.artifact_sha256,
            "timeout_seconds": self.timeout_seconds,
            "created_at": self.created_at,
            "agent_id": self.agent_id,
            "claimed_at": self.claimed_at,
            "completed_at": self.completed_at,
            "metadata": self.metadata,
            "report": self.report,
            "detail": self.detail,
        }

    def to_agent_dict(self, artifact_url: str) -> dict[str, Any]:
        """Trimmed JSON shape shipped to the Windows agent.

        Excludes the server-side ``artifact_path`` (filesystem leak)
        and adds the ``artifact_url`` the agent GETs to download the
        GBX. Same run_id + sha256 the enqueue CLI computed.
        """
        return {
            "id": self.id,
            "run_id": self.run_id,
            "status": self.status.value,
            "artifact_url": artifact_url,
            "artifact_size": self.artifact_size,
            "artifact_sha256": self.artifact_sha256,
            "timeout_seconds": self.timeout_seconds,
            "metadata": self.metadata,
        }


@dataclass
class AgentHeartbeat:
    """Liveness signal from a Windows agent — lets the operator see
    which rigs are online in the dashboard without needing to poll
    every job."""
    agent_id: str
    last_seen_at: int
    version: str | None = None
    hostname: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "last_seen_at": self.last_seen_at,
            "version": self.version,
            "hostname": self.hostname,
            "notes": self.notes,
        }


def _json_or_empty(s: str | None) -> dict[str, Any]:
    if not s:
        return {}
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else {}
    except (TypeError, ValueError):
        return {}


def _json_or_none(s: str | None) -> dict[str, Any] | None:
    if not s:
        return None
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else None
    except (TypeError, ValueError):
        return None
