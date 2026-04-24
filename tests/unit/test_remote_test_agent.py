"""Unit tests for the Windows agent.

The agent is OS-agnostic — Windows-ness is where paths point, not
how the loop works. We run the full lifecycle against a live Flask
test server on localhost + a mocked OpenPlanet plugin that writes
the expected .out.json.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

from src.remote_test.server import create_app
from src.remote_test.storage import JobStore
from src.remote_test_agent.agent import run_agent
from src.remote_test_agent.config import (
    AgentConfig,
    AgentIdentity,
    PathsConfig,
    PollingConfig,
    ServerConfig,
    load_config,
)
from src.remote_test_agent.http_client import (
    RemoteTestClient,
    RemoteTestHTTPError,
)
from src.remote_test_agent.plugin_io import (
    PROTOCOL_VERSION,
    PluginIO,
    PluginIOError,
    TelemetryReport,
    _parse_report,
)


TOKEN = "test-token-xyz"


# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------

class TestConfigLoader:
    def test_round_trip_yaml(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "agent.yaml"
        cfg_path.write_text(
            "server:\n"
            "  url: http://example.test:8787\n"
            "  token: literal-token\n"
            "agent:\n"
            "  id: winrig-xyz\n"
            "paths:\n"
            f"  tm_maps_root: {tmp_path / 'maps'}\n"
            "  ai_inbox_subdir: AI-inbox\n"
            f"  plugin_rig_dir: {tmp_path / 'rig'}\n",
            encoding="utf-8",
        )
        cfg = load_config(cfg_path)
        assert cfg.server.url == "http://example.test:8787"
        assert cfg.server.token == "literal-token"
        assert cfg.agent.id == "winrig-xyz"
        assert cfg.paths.ai_inbox_dir == tmp_path / "maps" / "AI-inbox"

    def test_env_token_overrides_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg_path = tmp_path / "agent.yaml"
        cfg_path.write_text(
            "server:\n  url: http://x:1\n  token: in-file\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("REMOTE_TEST_TOKEN", "from-env")
        cfg = load_config(cfg_path)
        assert cfg.server.token == "from-env"

    def test_missing_url_rejected(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "agent.yaml"
        cfg_path.write_text("agent:\n  id: x\n", encoding="utf-8")
        with pytest.raises(ValueError, match="server.url is required"):
            load_config(cfg_path)


# ---------------------------------------------------------------------
# PluginIO
# ---------------------------------------------------------------------

class TestPluginIO:
    def test_drop_trigger_writes_expected_shape(self, tmp_path: Path) -> None:
        plugin = PluginIO(tmp_path)
        map_file = tmp_path / "map.Map.Gbx"
        plugin.drop_trigger(
            job_id=7, run_id="run-42",
            map_file=map_file, deadline_unix=1_700_000_000,
            metadata={"foo": "bar"},
        )
        body = json.loads(
            (tmp_path / "7.in.json").read_text(encoding="utf-8"),
        )
        assert body["protocol"] == PROTOCOL_VERSION
        assert body["job_id"] == 7
        assert body["run_id"] == "run-42"
        assert body["map_file"].endswith("map.Map.Gbx")
        assert body["metadata"] == {"foo": "bar"}

    def test_wait_returns_report_when_file_appears(
        self, tmp_path: Path,
    ) -> None:
        plugin = PluginIO(tmp_path)
        # Have a background thread drop the plugin output after 0.3s.
        def _drop() -> None:
            time.sleep(0.3)
            (tmp_path / "9.out.json").write_text(json.dumps({
                "protocol": PROTOCOL_VERSION,
                "job_id": 9,
                "run_id": "r",
                "load_success": True,
                "finished": True,
                "exit_reason": "finished",
                "plugin_version": "plugin-v0.1",
            }), encoding="utf-8")
        threading.Thread(target=_drop).start()
        deadline = int(time.time()) + 5
        rep = plugin.wait_for_report(
            job_id=9, deadline_unix=deadline,
            poll_interval_s=0.1,
        )
        assert rep is not None
        assert rep.finished is True
        assert rep.exit_reason == "finished"
        assert rep.plugin_version == "plugin-v0.1"

    def test_wait_times_out_returns_none(self, tmp_path: Path) -> None:
        plugin = PluginIO(tmp_path)
        start = time.time()
        rep = plugin.wait_for_report(
            job_id=10,
            deadline_unix=int(start + 0.5),
            poll_interval_s=0.1,
        )
        assert rep is None
        assert time.time() - start < 2.0  # sanity: actually short

    def test_malformed_report_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "3.out.json").write_text(
            json.dumps({"protocol": "wrong", "job_id": 3}),
            encoding="utf-8",
        )
        with pytest.raises(PluginIOError, match="protocol mismatch"):
            _parse_report(tmp_path / "3.out.json")

    def test_ack_deletes_output(self, tmp_path: Path) -> None:
        plugin = PluginIO(tmp_path)
        (tmp_path / "4.out.json").write_text("{}", encoding="utf-8")
        plugin.ack(4)
        assert not (tmp_path / "4.out.json").exists()

    def test_clear_stale_removes_prior_pair(self, tmp_path: Path) -> None:
        plugin = PluginIO(tmp_path)
        for name in ("5.in.json", "5.out.json"):
            (tmp_path / name).write_text("x", encoding="utf-8")
        plugin.clear_stale(5)
        assert not (tmp_path / "5.in.json").exists()
        assert not (tmp_path / "5.out.json").exists()


# ---------------------------------------------------------------------
# HTTP client — driven by a live Flask test app on a real socket
# ---------------------------------------------------------------------

@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[tuple[str, JobStore]]:
    """Spin up the Flask app on localhost:0 (ephemeral port) so the
    agent's real requests-based HTTP client exercises the full path.
    """
    from werkzeug.serving import make_server
    store = JobStore(tmp_path / "jobs.db")
    app = create_app(
        store=store, artifacts_root=tmp_path / "art",
        auth_token=TOKEN, allow_insecure=False,
    )
    server = make_server("127.0.0.1", 0, app, threaded=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", store
    finally:
        server.shutdown()
        thread.join(timeout=2)
        store.close()


class TestHTTPClient:
    def test_ping_health(
        self, live_server: tuple[str, JobStore],
    ) -> None:
        url, _ = live_server
        client = RemoteTestClient(server_url=url, token=TOKEN)
        assert client.ping_health() is True

    def test_claim_returns_none_on_empty(
        self, live_server: tuple[str, JobStore],
    ) -> None:
        url, _ = live_server
        client = RemoteTestClient(server_url=url, token=TOKEN)
        assert client.claim_next("agent-1") is None

    def test_wrong_token_raises(
        self, live_server: tuple[str, JobStore],
    ) -> None:
        url, _ = live_server
        client = RemoteTestClient(server_url=url, token="wrong")
        with pytest.raises(RemoteTestHTTPError) as exc_info:
            client.claim_next("agent-1")
        assert exc_info.value.status_code == 401

    def test_download_roundtrip(
        self, live_server: tuple[str, JobStore], tmp_path: Path,
    ) -> None:
        url, store = live_server
        r = store.enqueue(
            run_id="dl-test",
            artifact_bytes=b"HELLO-GBX",
            artifacts_root=Path(store._path).parent / "art",  # type: ignore[attr-defined]
        )
        client = RemoteTestClient(server_url=url, token=TOKEN)
        claimed = client.claim_next("dl-agent")
        assert claimed is not None
        dest = tmp_path / "out.Map.Gbx"
        n = client.download_artifact(
            url=claimed.artifact_url, destination=dest,
        )
        assert n == claimed.artifact_size
        assert dest.read_bytes() == b"HELLO-GBX"


# ---------------------------------------------------------------------
# Full agent loop end-to-end
# ---------------------------------------------------------------------


def _make_agent_config(
    *,
    server_url: str,
    tmp_path: Path,
    plugin_rig_dir: Path,
    ai_inbox: Path,
) -> AgentConfig:
    return AgentConfig(
        server=ServerConfig(url=server_url, token=TOKEN),
        agent=AgentIdentity(
            id="test-agent", version="t0",
            hostname="test-host",
        ),
        paths=PathsConfig(
            tm_maps_root=ai_inbox.parent,
            ai_inbox_subdir=ai_inbox.name,
            plugin_rig_dir=plugin_rig_dir,
        ),
        polling=PollingConfig(
            queue_interval_s=0.2,
            heartbeat_interval_s=10.0,
            plugin_poll_interval_s=0.1,
            plugin_wait_max_extra_s=2.0,
        ),
    )


class TestAgentLoop:
    def test_full_lifecycle_with_fake_plugin(
        self, live_server: tuple[str, JobStore], tmp_path: Path,
    ) -> None:
        url, store = live_server

        # Enqueue a job directly via the store (bypasses the HTTP
        # enqueue path which needs multipart — already covered
        # elsewhere).
        r = store.enqueue(
            run_id="e2e-run",
            artifact_bytes=b"END-TO-END-GBX",
            artifacts_root=Path(store._path).parent / "art",  # type: ignore[attr-defined]
            metadata={"base_map_id": 1212},
            timeout_seconds=5,
        )

        inbox = tmp_path / "maps" / "AI-inbox"
        rig = tmp_path / "rig"
        cfg = _make_agent_config(
            server_url=url, tmp_path=tmp_path,
            plugin_rig_dir=rig, ai_inbox=inbox,
        )

        # A fake plugin thread: waits for <id>.in.json, writes
        # <id>.out.json with a canned report.
        def _fake_plugin() -> None:
            deadline = time.time() + 5.0
            in_path = rig / f"{r.job_id}.in.json"
            while time.time() < deadline:
                if in_path.exists():
                    time.sleep(0.05)  # let the agent finish the write
                    (rig / f"{r.job_id}.out.json").write_text(
                        json.dumps({
                            "protocol": PROTOCOL_VERSION,
                            "job_id": r.job_id,
                            "run_id": "e2e-run",
                            "load_success": True,
                            "spawn_ok": True,
                            "finished": True,
                            "checkpoint_times_ms": [3200, 6400],
                            "driven_cells": [[0, 9, 0], [1, 9, 0]],
                            "exit_reason": "finished",
                            "plugin_version": "fake-plugin-0.0",
                        }), encoding="utf-8",
                    )
                    return
                time.sleep(0.1)

        threading.Thread(target=_fake_plugin, daemon=True).start()

        # Run at most N iterations; the lifecycle should resolve
        # on the first hit.
        exit_code = run_agent(cfg, max_iterations=6)
        assert exit_code == 0

        # Job is complete + report present on the server side.
        job = store.get_strict(r.job_id)
        assert job.status.value == "complete"
        assert job.report is not None
        assert job.report["finished"] is True
        assert job.report["checkpoint_times_ms"] == [3200, 6400]
        assert job.report["driven_cells_head"][0] == [0, 9, 0]
        # The artifact was staged into the inbox with the correct name.
        assert (inbox / "e2e-run.Map.Gbx").read_bytes() == b"END-TO-END-GBX"

    def test_plugin_timeout_reports_timed_out(
        self, live_server: tuple[str, JobStore], tmp_path: Path,
    ) -> None:
        url, store = live_server
        r = store.enqueue(
            run_id="no-plugin",
            artifact_bytes=b"X",
            artifacts_root=Path(store._path).parent / "art",  # type: ignore[attr-defined]
            timeout_seconds=1,   # tight deadline
        )
        inbox = tmp_path / "maps" / "AI-inbox"
        rig = tmp_path / "rig"
        cfg = _make_agent_config(
            server_url=url, tmp_path=tmp_path,
            plugin_rig_dir=rig, ai_inbox=inbox,
        )
        # No fake plugin — the agent should time out waiting.
        exit_code = run_agent(cfg, max_iterations=5)
        assert exit_code == 0
        job = store.get_strict(r.job_id)
        assert job.status.value == "timed_out"
        assert "did not respond" in (job.detail or "")

    def test_server_unreachable_fails_startup(
        self, tmp_path: Path,
    ) -> None:
        cfg = _make_agent_config(
            server_url="http://127.0.0.1:1",   # definitely closed
            tmp_path=tmp_path,
            plugin_rig_dir=tmp_path / "rig",
            ai_inbox=tmp_path / "m" / "AI-inbox",
        )
        assert run_agent(cfg, max_iterations=1) == 2
