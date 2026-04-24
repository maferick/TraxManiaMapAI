"""Flask WSGI app for the remote-test queue.

Endpoints
---------

Agent-facing (always require bearer auth):

  GET  /health                         — {"ok": true, "service": "..."}
  GET  /jobs/next?agent_id=...         — claim oldest queued job
                                          or 204 if none
  GET  /jobs/{id}/artifact             — stream the .Map.Gbx bytes
  POST /jobs/{id}/status               — body: {status, detail?, report?}
  POST /agents/{agent_id}/heartbeat    — body: {version, hostname, notes}

Operator / CLI side:

  POST /jobs                           — multipart: artifact + metadata
  GET  /jobs/{id}                      — full job state for polling
  GET  /jobs?limit=N                   — recent jobs
  GET  /agents                         — live agent list

Auth
----

Single shared bearer token. The server reads
``REMOTE_TEST_TOKEN`` from the environment at start; every request
(except ``/health``) must carry ``Authorization: Bearer <token>``.

Running ``--allow-insecure`` disables auth for local dev only; a
warning logs at startup. Do NOT run with ``--allow-insecure`` on a
LAN-reachable interface.

Security surface
----------------

- Artifact downloads are keyed on server-controlled filenames
  (SHA-256 hex); no path traversal possible.
- Metadata is stored as an opaque JSON blob — never executed, never
  reflected into paths or log format strings.
- Multipart upload size capped at ``MAX_UPLOAD_BYTES`` (default
  16 MiB) to stop accidental DoS via giant uploads.
- All state writes go through :class:`JobStore.transition` which
  enforces a closed transition graph.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, request, send_file

from src.remote_test.models import Job, JobStatus
from src.remote_test.storage import (
    EnqueueResult,
    JobStore,
    JobStoreError,
)

_LOG = logging.getLogger(__name__)

# 16 MiB cap on uploads. Real TM2020 maps are < 4 MiB in practice;
# raising this to handle monster custom uploads is a config change,
# not a bug to fix in the parser.
MAX_UPLOAD_BYTES: int = 16 * 1024 * 1024

_BEARER_PREFIX = "Bearer "


def create_app(
    *,
    store: JobStore,
    artifacts_root: Path,
    auth_token: str | None,
    allow_insecure: bool = False,
) -> Flask:
    """Build the Flask app. ``auth_token`` required unless
    ``allow_insecure`` is True (dev-only; logs a warning at startup).
    """
    app = Flask("remote_test_server")
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    artifacts_root = Path(artifacts_root)
    artifacts_root.mkdir(parents=True, exist_ok=True)

    if not auth_token and not allow_insecure:
        raise RuntimeError(
            "auth_token is required — set REMOTE_TEST_TOKEN or pass "
            "--allow-insecure for local dev only"
        )
    if allow_insecure:
        _LOG.warning(
            "remote-test server running with auth DISABLED "
            "(--allow-insecure). Do not expose on a LAN."
        )

    # ---------- auth middleware ----------

    def _authorised() -> bool:
        if allow_insecure:
            return True
        header = request.headers.get("Authorization", "")
        if not header.startswith(_BEARER_PREFIX):
            return False
        candidate = header[len(_BEARER_PREFIX):].strip()
        return bool(auth_token) and candidate == auth_token

    @app.before_request
    def _gate() -> Response | None:
        if request.path == "/health":
            return None
        if not _authorised():
            return jsonify({"error": "unauthorised"}), 401
        return None

    # ---------- endpoints ----------

    @app.get("/health")
    def health() -> Response:
        return jsonify({
            "ok": True,
            "service": "remote-test-server",
            "auth": "enabled" if not allow_insecure else "disabled",
        })

    @app.post("/jobs")
    def enqueue_job() -> Response:
        """Multipart form:
          - ``artifact`` file part (the .Map.Gbx)
          - ``run_id``  form field
          - ``metadata`` form field (JSON, optional)
          - ``timeout_seconds`` form field (int, optional)
        """
        artifact = request.files.get("artifact")
        if artifact is None:
            return jsonify({"error": "artifact file part required"}), 400
        run_id = request.form.get("run_id", "").strip()
        if not run_id:
            return jsonify({"error": "run_id required"}), 400
        metadata_raw = request.form.get("metadata", "{}")
        try:
            metadata = json.loads(metadata_raw)
            if not isinstance(metadata, dict):
                raise ValueError("metadata must be a JSON object")
        except (TypeError, ValueError) as exc:
            return jsonify({"error": f"bad metadata: {exc}"}), 400
        try:
            timeout_seconds = int(request.form.get("timeout_seconds", "300"))
        except (TypeError, ValueError):
            return jsonify({"error": "timeout_seconds must be int"}), 400

        data = artifact.read()
        try:
            res: EnqueueResult = store.enqueue(
                run_id=run_id,
                artifact_bytes=data,
                artifacts_root=artifacts_root,
                metadata=metadata,
                timeout_seconds=timeout_seconds,
            )
        except JobStoreError as exc:
            return jsonify({"error": str(exc)}), 400

        job = store.get_strict(res.job_id)
        return jsonify(job.to_dict()), 201

    @app.get("/jobs/next")
    def claim_next_job() -> Response:
        agent_id = request.args.get("agent_id", "").strip()
        if not agent_id:
            return jsonify({"error": "agent_id query param required"}), 400
        # Sweep stale claims so a dead agent can't hold a job forever.
        store.sweep_timeouts()
        job = store.claim_next(agent_id=agent_id)
        if job is None:
            return Response(status=204)
        artifact_url = (
            f"{request.url_root.rstrip('/')}/jobs/{job.id}/artifact"
        )
        return jsonify(job.to_agent_dict(artifact_url))

    @app.get("/jobs/<int:job_id>/artifact")
    def download_artifact(job_id: int) -> Response:
        job = store.get(job_id)
        if job is None:
            abort(404)
        path = artifacts_root / job.artifact_path
        if not path.exists():
            abort(410)  # artifact was purged
        return send_file(
            str(path),
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=f"{job.run_id}.Map.Gbx",
        )

    @app.post("/jobs/<int:job_id>/status")
    def post_status(job_id: int) -> Response:
        body = request.get_json(silent=True) or {}
        status_raw = body.get("status")
        if not status_raw:
            return jsonify({"error": "status required"}), 400
        try:
            to_status = JobStatus(status_raw)
        except ValueError:
            return jsonify({"error": f"unknown status: {status_raw!r}"}), 400
        agent_id = body.get("agent_id")
        detail = body.get("detail")
        report = body.get("report")
        if report is not None and not isinstance(report, dict):
            return jsonify({"error": "report must be an object"}), 400
        try:
            job = store.transition(
                job_id=job_id, to_status=to_status,
                agent_id=agent_id, detail=detail, report=report,
            )
        except JobStoreError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(job.to_dict())

    @app.post("/agents/<string:agent_id>/heartbeat")
    def post_heartbeat(agent_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        hb = store.upsert_agent_heartbeat(
            agent_id=agent_id,
            version=body.get("version"),
            hostname=body.get("hostname"),
            notes=body.get("notes"),
        )
        return jsonify(hb.to_dict())

    @app.get("/jobs/<int:job_id>")
    def get_job(job_id: int) -> Response:
        job = store.get(job_id)
        if job is None:
            abort(404)
        return jsonify(job.to_dict())

    @app.get("/jobs")
    def list_jobs() -> Response:
        try:
            limit = int(request.args.get("limit", "50"))
        except (TypeError, ValueError):
            return jsonify({"error": "limit must be int"}), 400
        jobs = store.list_recent(limit=max(1, min(500, limit)))
        return jsonify({"jobs": [j.to_dict() for j in jobs]})

    @app.get("/agents")
    def list_agents() -> Response:
        return jsonify({
            "agents": [a.to_dict() for a in store.list_agents()],
        })

    @app.errorhandler(413)
    def too_large(_err: Any) -> Response:
        return jsonify({
            "error": f"payload exceeds MAX_UPLOAD_BYTES ({MAX_UPLOAD_BYTES})",
        }), 413

    return app


def resolve_auth_token(cli_token: str | None) -> str | None:
    """Priority: CLI flag > env var > None."""
    if cli_token:
        return cli_token
    env = os.environ.get("REMOTE_TEST_TOKEN")
    if env:
        return env.strip() or None
    return None
