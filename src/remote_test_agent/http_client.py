"""Thin HTTP wrapper around the Linux queue server.

All calls carry the bearer token from config. Surfaces 404/410
distinctly so the agent can decide: retry vs abandon.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

_LOG = logging.getLogger(__name__)


class RemoteTestHTTPError(Exception):
    """Non-fatal server-side error. Agent logs + retries."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"HTTP {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body


@dataclass
class ClaimedJob:
    id: int
    run_id: str
    artifact_url: str
    artifact_size: int
    artifact_sha256: str
    timeout_seconds: int
    metadata: dict[str, Any]


class RemoteTestClient:
    """Session-backed HTTP client. One instance per agent process."""

    def __init__(
        self,
        *,
        server_url: str,
        token: str | None,
        verify_tls: bool = True,
        request_timeout_s: float = 30.0,
    ) -> None:
        self._base = server_url.rstrip("/")
        self._session = requests.Session()
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"
        self._session.verify = verify_tls
        self._request_timeout = request_timeout_s

    # ------------- agent-facing calls -------------

    def claim_next(self, agent_id: str) -> ClaimedJob | None:
        """Returns ``None`` when the queue is empty (HTTP 204)."""
        r = self._session.get(
            f"{self._base}/jobs/next",
            params={"agent_id": agent_id},
            timeout=self._request_timeout,
        )
        if r.status_code == 204:
            return None
        if r.status_code != 200:
            raise RemoteTestHTTPError(r.status_code, r.text)
        b = r.json()
        return ClaimedJob(
            id=int(b["id"]),
            run_id=str(b["run_id"]),
            artifact_url=str(b["artifact_url"]),
            artifact_size=int(b["artifact_size"]),
            artifact_sha256=str(b["artifact_sha256"]),
            timeout_seconds=int(b["timeout_seconds"]),
            metadata=dict(b.get("metadata") or {}),
        )

    def download_artifact(
        self, url: str, *, destination: Path,
    ) -> int:
        """Stream the artifact to disk. Returns byte count."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._session.get(
            url, stream=True, timeout=self._request_timeout,
        ) as r:
            if r.status_code != 200:
                raise RemoteTestHTTPError(r.status_code, r.text)
            n = 0
            with destination.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    n += len(chunk)
        return n

    def post_status(
        self,
        *,
        job_id: int,
        status: str,
        agent_id: str,
        detail: str | None = None,
        report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"status": status, "agent_id": agent_id}
        if detail is not None:
            body["detail"] = detail
        if report is not None:
            body["report"] = report
        r = self._session.post(
            f"{self._base}/jobs/{int(job_id)}/status",
            json=body, timeout=self._request_timeout,
        )
        if r.status_code not in (200, 409):
            raise RemoteTestHTTPError(r.status_code, r.text)
        # 409 = state-transition conflict; return it so the agent
        # can log + move on without crashing.
        return r.json()

    def post_heartbeat(
        self,
        *,
        agent_id: str,
        version: str | None = None,
        hostname: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        body = {
            k: v for k, v in
            {"version": version, "hostname": hostname, "notes": notes}.items()
            if v is not None
        }
        r = self._session.post(
            f"{self._base}/agents/{agent_id}/heartbeat",
            json=body, timeout=self._request_timeout,
        )
        if r.status_code != 200:
            raise RemoteTestHTTPError(r.status_code, r.text)
        return r.json()

    def ping_health(self) -> bool:
        """Cheap reachability check — used at agent startup to
        surface config / network errors early."""
        try:
            r = self._session.get(
                f"{self._base}/health", timeout=5.0,
            )
            return r.status_code == 200
        except requests.RequestException:
            return False
