"""Flask test-client coverage for the remote-test server.

End-to-end-ish: exercises every endpoint through the Flask test
client (no real network), checks auth, state transitions, and
artifact round-trip.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
from flask.testing import FlaskClient

from src.remote_test.server import create_app
from src.remote_test.storage import JobStore


TOKEN = "test-token-xyz"


@pytest.fixture
def app_with_store(tmp_path: Path) -> Iterator[tuple[FlaskClient, JobStore]]:
    store = JobStore(tmp_path / "jobs.db")
    artifacts_root = tmp_path / "artifacts"
    app = create_app(
        store=store, artifacts_root=artifacts_root,
        auth_token=TOKEN, allow_insecure=False,
    )
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client, store
    store.close()


def _auth(extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {TOKEN}"}
    if extra:
        h.update(extra)
    return h


class TestHealth:
    def test_no_auth_needed(
        self, app_with_store: tuple[FlaskClient, JobStore],
    ) -> None:
        client, _ = app_with_store
        r = client.get("/health")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["auth"] == "enabled"


class TestAuthGate:
    def test_missing_token_rejects(
        self, app_with_store: tuple[FlaskClient, JobStore],
    ) -> None:
        client, _ = app_with_store
        r = client.get("/jobs")
        assert r.status_code == 401

    def test_wrong_token_rejects(
        self, app_with_store: tuple[FlaskClient, JobStore],
    ) -> None:
        client, _ = app_with_store
        r = client.get("/jobs", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401

    def test_correct_token_admitted(
        self, app_with_store: tuple[FlaskClient, JobStore],
    ) -> None:
        client, _ = app_with_store
        r = client.get("/jobs", headers=_auth())
        assert r.status_code == 200


class TestAllowInsecure:
    def test_insecure_skips_auth(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        app = create_app(
            store=store, artifacts_root=tmp_path / "art",
            auth_token=None, allow_insecure=True,
        )
        app.config["TESTING"] = True
        with app.test_client() as client:
            r = client.get("/jobs")  # no Authorization header
            assert r.status_code == 200
        store.close()

    def test_missing_token_not_insecure_refuses_boot(
        self, tmp_path: Path,
    ) -> None:
        store = JobStore(tmp_path / "jobs.db")
        with pytest.raises(RuntimeError, match="auth_token"):
            create_app(
                store=store, artifacts_root=tmp_path / "art",
                auth_token=None, allow_insecure=False,
            )
        store.close()


class TestEnqueueAndClaim:
    def test_enqueue_multipart_creates_queued(
        self, app_with_store: tuple[FlaskClient, JobStore],
    ) -> None:
        client, store = app_with_store
        meta = {"base_map_id": 1212, "random_seed": 42}
        r = client.post(
            "/jobs",
            headers=_auth(),
            data={
                "run_id": "abc123",
                "metadata": json.dumps(meta),
                "timeout_seconds": "60",
                "artifact": (
                    __import__("io").BytesIO(b"FAKE_GBX_BYTES"),
                    "test.Map.Gbx",
                    "application/octet-stream",
                ),
            },
            content_type="multipart/form-data",
        )
        assert r.status_code == 201, r.get_json()
        body = r.get_json()
        assert body["run_id"] == "abc123"
        assert body["status"] == "queued"
        assert body["metadata"] == meta

    def test_claim_next_returns_204_when_empty(
        self, app_with_store: tuple[FlaskClient, JobStore],
    ) -> None:
        client, _ = app_with_store
        r = client.get("/jobs/next?agent_id=a1", headers=_auth())
        assert r.status_code == 204

    def test_full_lifecycle(
        self, app_with_store: tuple[FlaskClient, JobStore],
    ) -> None:
        client, store = app_with_store
        # Enqueue
        r = client.post(
            "/jobs",
            headers=_auth(),
            data={
                "run_id": "life-cycle",
                "artifact": (
                    __import__("io").BytesIO(b"DATA"),
                    "t.Map.Gbx", "application/octet-stream",
                ),
            },
            content_type="multipart/form-data",
        )
        assert r.status_code == 201
        job_id = r.get_json()["id"]

        # Claim
        r = client.get(
            f"/jobs/next?agent_id=winrig-1", headers=_auth(),
        )
        assert r.status_code == 200
        agent_view = r.get_json()
        assert agent_view["id"] == job_id
        assert agent_view["status"] == "claimed"
        assert "artifact_url" in agent_view

        # Artifact download
        r = client.get(
            f"/jobs/{job_id}/artifact", headers=_auth(),
        )
        assert r.status_code == 200
        assert r.data == b"DATA"

        # Running
        r = client.post(
            f"/jobs/{job_id}/status", headers=_auth(),
            json={"status": "running", "agent_id": "winrig-1",
                  "detail": "map loading"},
        )
        assert r.status_code == 200
        assert r.get_json()["status"] == "running"

        # Complete with report
        r = client.post(
            f"/jobs/{job_id}/status", headers=_auth(),
            json={
                "status": "complete", "agent_id": "winrig-1",
                "report": {
                    "load_success": True, "finished": False,
                    "driven_cells": 42,
                },
            },
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["status"] == "complete"
        assert body["report"]["driven_cells"] == 42
        assert body["completed_at"] is not None

    def test_claim_by_wrong_agent_is_rejected(
        self, app_with_store: tuple[FlaskClient, JobStore],
    ) -> None:
        client, _ = app_with_store
        r = client.post(
            "/jobs", headers=_auth(),
            data={
                "run_id": "rej",
                "artifact": (
                    __import__("io").BytesIO(b"X"),
                    "t.Map.Gbx", "application/octet-stream",
                ),
            },
            content_type="multipart/form-data",
        )
        job_id = r.get_json()["id"]
        client.get("/jobs/next?agent_id=a1", headers=_auth())
        r = client.post(
            f"/jobs/{job_id}/status", headers=_auth(),
            json={"status": "running", "agent_id": "a2"},
        )
        assert r.status_code == 409  # claimed-by mismatch
        assert "claimed by" in r.get_json()["error"]

    def test_invalid_status_value_rejected(
        self, app_with_store: tuple[FlaskClient, JobStore],
    ) -> None:
        client, _ = app_with_store
        r = client.post(
            "/jobs", headers=_auth(),
            data={
                "run_id": "bad",
                "artifact": (
                    __import__("io").BytesIO(b"X"),
                    "t.Map.Gbx", "application/octet-stream",
                ),
            },
            content_type="multipart/form-data",
        )
        job_id = r.get_json()["id"]
        r = client.post(
            f"/jobs/{job_id}/status", headers=_auth(),
            json={"status": "not-a-real-state"},
        )
        assert r.status_code == 400


class TestHeartbeats:
    def test_heartbeat_upsert_and_listing(
        self, app_with_store: tuple[FlaskClient, JobStore],
    ) -> None:
        client, _ = app_with_store
        r = client.post(
            "/agents/winrig-1/heartbeat", headers=_auth(),
            json={"version": "0.1", "hostname": "gaming-pc"},
        )
        assert r.status_code == 200
        r = client.get("/agents", headers=_auth())
        body = r.get_json()
        assert len(body["agents"]) == 1
        assert body["agents"][0]["agent_id"] == "winrig-1"
        assert body["agents"][0]["hostname"] == "gaming-pc"
