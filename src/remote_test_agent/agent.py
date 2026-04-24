"""Main agent loop.

One process, one job at a time. Idle → poll queue → on hit:
download → stage map → drop plugin trigger → wait for plugin →
ship report → idle. Heartbeats run on a simple interval check
inside the idle loop (no separate thread).

The loop keeps going on any single-job error — an interval's
failure should never take the whole rig offline. Only
unrecoverable config / auth errors raise out of run_agent().
"""
from __future__ import annotations

import logging
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from src.remote_test_agent.config import AgentConfig
from src.remote_test_agent.http_client import (
    ClaimedJob,
    RemoteTestClient,
    RemoteTestHTTPError,
)
from src.remote_test_agent.plugin_io import (
    PluginIO,
    TelemetryReport,
)

_LOG = logging.getLogger(__name__)


@dataclass
class _LoopState:
    last_heartbeat_at: float = 0.0
    shutdown_requested: bool = False


def run_agent(
    cfg: AgentConfig,
    *,
    max_iterations: int | None = None,
) -> int:
    """Run the agent forever (or for ``max_iterations`` idle/work
    cycles — useful for tests).

    Returns the exit code: 0 on clean shutdown, non-zero on
    startup failure (bad server URL etc.).
    """
    _validate_paths(cfg)

    client = RemoteTestClient(
        server_url=cfg.server.url,
        token=cfg.server.token,
        verify_tls=cfg.server.verify_tls,
    )
    plugin = PluginIO(cfg.paths.plugin_rig_dir)

    if not client.ping_health():
        _LOG.error(
            "remote-test server at %s unreachable or failing health check",
            cfg.server.url,
        )
        return 2

    state = _LoopState()
    _install_signal_handler(state)

    _LOG.info(
        "remote-test agent %s starting — server=%s inbox=%s plugin_dir=%s",
        cfg.agent.id, cfg.server.url, cfg.paths.ai_inbox_dir,
        cfg.paths.plugin_rig_dir,
    )

    iterations = 0
    while not state.shutdown_requested:
        iterations += 1
        _maybe_heartbeat(client, cfg, state)
        try:
            job = client.claim_next(cfg.agent.id)
        except RemoteTestHTTPError as exc:
            _LOG.warning("claim_next failed: %s", exc)
            job = None
        if job is not None:
            try:
                _handle_job(client, plugin, cfg, job)
            except Exception as exc:  # keep the loop alive
                _LOG.exception("job %d raised — continuing: %s", job.id, exc)
        else:
            time.sleep(cfg.polling.queue_interval_s)
        if max_iterations is not None and iterations >= max_iterations:
            break

    _LOG.info("remote-test agent shutting down cleanly")
    return 0


# ---------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------

def _validate_paths(cfg: AgentConfig) -> None:
    # Create what we can — fail fast on things we can't create.
    cfg.paths.ai_inbox_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.plugin_rig_dir.mkdir(parents=True, exist_ok=True)


def _install_signal_handler(state: _LoopState) -> None:
    def _stop(signum: int, _frame) -> None:
        _LOG.info("signal %d received — finishing current job", signum)
        state.shutdown_requested = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            # Some environments (tests, Windows services) don't allow
            # installing handlers. Ignore — the loop still exits via
            # max_iterations or external kill.
            pass


def _maybe_heartbeat(
    client: RemoteTestClient, cfg: AgentConfig, state: _LoopState,
) -> None:
    now = time.time()
    if now - state.last_heartbeat_at < cfg.polling.heartbeat_interval_s:
        return
    try:
        client.post_heartbeat(
            agent_id=cfg.agent.id,
            version=cfg.agent.version,
            hostname=cfg.agent.hostname,
        )
        state.last_heartbeat_at = now
    except RemoteTestHTTPError as exc:
        _LOG.warning("heartbeat failed: %s", exc)


def _handle_job(
    client: RemoteTestClient,
    plugin: PluginIO,
    cfg: AgentConfig,
    job: ClaimedJob,
) -> None:
    _LOG.info(
        "job %d claimed: run_id=%s size=%d sha256=%s…",
        job.id, job.run_id, job.artifact_size, job.artifact_sha256[:12],
    )
    inbox_map = cfg.paths.ai_inbox_dir / f"{job.run_id}.Map.Gbx"
    # Clean any stale plugin files BEFORE dropping the trigger so
    # we don't read a previous run's .out.json.
    plugin.clear_stale(job.id)

    # Download the artifact + verify sha. Mismatches → fail the
    # job immediately rather than hand a tampered file to TM2020.
    try:
        got = client.download_artifact(
            url=job.artifact_url, destination=inbox_map,
        )
    except RemoteTestHTTPError as exc:
        _report_fail(client, cfg, job, f"artifact download failed: {exc}")
        return
    if got != job.artifact_size:
        _report_fail(
            client, cfg, job,
            f"size mismatch: expected {job.artifact_size} got {got}",
        )
        return
    import hashlib
    digest = hashlib.sha256(inbox_map.read_bytes()).hexdigest()
    if digest != job.artifact_sha256:
        _report_fail(
            client, cfg, job,
            f"sha256 mismatch: expected {job.artifact_sha256} got {digest}",
        )
        return

    # Transition to RUNNING before we drop the trigger — gives
    # operators visibility while the plugin chews on the map.
    try:
        client.post_status(
            job_id=job.id, status="running", agent_id=cfg.agent.id,
            detail="artifact staged; plugin trigger dropped",
        )
    except RemoteTestHTTPError as exc:
        _LOG.warning("post_status(running) failed: %s", exc)

    deadline = int(time.time()) + int(
        job.timeout_seconds + cfg.polling.plugin_wait_max_extra_s
    )
    plugin.drop_trigger(
        job_id=job.id, run_id=job.run_id,
        map_file=inbox_map, deadline_unix=deadline,
        metadata=job.metadata,
    )

    report = plugin.wait_for_report(
        job_id=job.id,
        deadline_unix=deadline,
        poll_interval_s=cfg.polling.plugin_poll_interval_s,
    )
    if report is None:
        _report_fail(
            client, cfg, job,
            f"plugin did not respond within {deadline - int(time.time())}s",
            status="timed_out",
        )
        return
    plugin.ack(job.id)
    _ship_report(client, cfg, job, report)


def _ship_report(
    client: RemoteTestClient,
    cfg: AgentConfig,
    job: ClaimedJob,
    report: TelemetryReport,
) -> None:
    detail = _summarise_report(report)
    try:
        client.post_status(
            job_id=job.id,
            status="complete",
            agent_id=cfg.agent.id,
            detail=detail,
            report=report.to_report_dict(),
        )
        _LOG.info("job %d → complete: %s", job.id, detail)
    except RemoteTestHTTPError as exc:
        _LOG.error("post_status(complete) failed: %s", exc)


def _report_fail(
    client: RemoteTestClient,
    cfg: AgentConfig,
    job: ClaimedJob,
    detail: str,
    *,
    status: str = "failed",
) -> None:
    _LOG.warning("job %d → %s: %s", job.id, status, detail)
    try:
        client.post_status(
            job_id=job.id, status=status, agent_id=cfg.agent.id,
            detail=detail,
        )
    except RemoteTestHTTPError as exc:
        _LOG.error("post_status(%s) failed: %s", status, exc)


def _summarise_report(report: TelemetryReport) -> str:
    bits = [
        f"load={'ok' if report.load_success else 'fail'}",
        f"spawn={'ok' if report.spawn_ok else 'fail'}",
        f"finished={report.finished}",
    ]
    if report.validation_status is not None:
        bits.append(f"validation={report.validation_status}")
    if report.author_time_ms is not None:
        bits.append(f"author_time_ms={report.author_time_ms}")
    bits.extend([
        f"cps={len(report.checkpoint_times_ms)}",
        f"cells={len(report.driven_cells)}",
        f"exit={report.exit_reason}",
    ])
    if report.load_error:
        bits.append(f"load_error={report.load_error!r}")
    return " ".join(bits)
