"""Flask app for the web dashboard + Phase-2 operator control.

Reuses :mod:`tools.dashboard.state` — the decision-layer data model
is presentation-agnostic (pure Python, no UI dep). The TUI renders
via Rich markup; this app renders via Jinja templates. Both surface
the same panels (health, coverage, bottlenecks, freshness, learning,
diversity, next-action, counters).

Phase 2 adds a control layer — the dashboard is no longer read-only.
Operators trigger pipeline actions (Add Maps / Run Pipeline / Train
AI / Score / Generate-stub) via HTTP. The action catalogue + worker
lives in :mod:`tools.dashboard_web.actions`.

Routes:
  GET  /                          HTML dashboard + control panel
  GET  /api/state                 JSON snapshot
  GET  /healthz                   cheap liveness (no DB)
  GET  /api/actions               JSON action catalogue
  GET  /api/actions/status        JSON current + last-completed run
  POST /api/actions/<name>        kick off action <name>
  GET  /api/actions/<id>/log      Server-Sent Events stream of stdout

Auto-refresh of the state snapshot is HTML ``<meta http-equiv="refresh">``
— simplest correct answer at a 10-second cadence. Action logs stream
via SSE only while an action is in flight.

Deliberately simple:
- No auth. Bind to localhost by default; set DASHBOARD_HOST for LAN.
- Single-action-at-a-time lock. The 4 GB host can't parallelise heavy
  pipeline stages (see project_pipeline_memory_budget.md). Second
  action request while one is running returns HTTP 409.
- No persistent queue. A Flask restart kills in-flight actions; the
  operator re-triggers.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from tools.dashboard_web.actions import ACTIONS, ActionWorker, tail_stream

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_LOG = logging.getLogger(__name__)


def _fetch_state_sync() -> "DashboardState":
    """Gather a :class:`DashboardState` snapshot from the DB. Lazy-
    imports the project adapters so the Flask app imports cleanly
    even when the DB is unreachable — collection failure goes into
    the ``error`` field rather than raising."""
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from src.storage.mariadb import open_connection
        from src.utils.config import load_config
        from tools.dashboard.state import DashboardState, fetch_state
    except Exception as exc:  # noqa: BLE001
        from tools.dashboard.state import DashboardState
        return DashboardState(
            collected_at=datetime.now(tz=timezone.utc),
            error=f"import failed: {exc}",
        )
    try:
        conn = open_connection(load_config(None))
    except Exception as exc:  # noqa: BLE001
        return DashboardState(
            collected_at=datetime.now(tz=timezone.utc),
            error=f"db connect failed: {exc}",
        )
    try:
        return fetch_state(conn)
    finally:
        conn.close()


def _humanize_age_filter(ts: datetime | None) -> str:
    """Jinja filter mirroring the TUI's age formatting."""
    from tools.dashboard.render import _humanize_age
    return _humanize_age(ts)


def _percent_filter(num: float | int, denom: float | int) -> str:
    if not denom:
        return "—"
    return f"{(num / denom) * 100:.0f}%"


def _state_to_dict(state: "DashboardState") -> dict[str, Any]:
    """Serialize a DashboardState for the JSON endpoint. ``asdict``
    unpacks the dataclasses; we coerce datetimes to ISO strings so
    the payload is JSON-native."""
    raw = asdict(state)
    # datetimes to ISO
    raw["collected_at"] = state.collected_at.isoformat()
    for entry in raw.get("freshness") or []:
        ts = entry.get("completed_at")
        if hasattr(ts, "isoformat"):
            entry["completed_at"] = ts.isoformat()
    return raw


def _serialize_catalogue() -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "title": spec.title,
            "hint": spec.hint,
            "expected_minutes": spec.expected_minutes,
        }
        for spec in ACTIONS.values()
    ]


def _serialize_run(run: Any) -> dict[str, Any] | None:
    if run is None:
        return None
    return run.to_dict()


def _find_run(worker: ActionWorker, run_id: str) -> Any:
    cur = worker.current
    if cur is not None and cur.id == run_id:
        return cur
    last = worker.last_completed
    if last is not None and last.id == run_id:
        return last
    return None


def create_app(
    *,
    state_fetcher: Any = None,
    refresh_seconds: int = 10,
    worker: ActionWorker | None = None,
) -> Flask:
    """Construct a Flask app. ``state_fetcher`` is an override point
    for tests so they can inject a stub without hitting the DB.
    ``refresh_seconds`` tunes the HTML meta-refresh cadence.
    ``worker`` is a dependency-injection point for tests to supply a
    stubbed :class:`ActionWorker`."""
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    fetch = state_fetcher or _fetch_state_sync
    action_worker = worker or ActionWorker()
    app.jinja_env.filters["humanize_age"] = _humanize_age_filter
    app.jinja_env.filters["percent"] = _percent_filter

    @app.route("/")
    def dashboard():  # noqa: ANN202
        state = fetch()
        return render_template(
            "dashboard.html",
            state=state,
            refresh_seconds=refresh_seconds,
            actions=_serialize_catalogue(),
            current_run=_serialize_run(action_worker.current),
            last_run=_serialize_run(action_worker.last_completed),
        )

    @app.route("/api/state")
    def api_state():  # noqa: ANN202
        return jsonify(_state_to_dict(fetch()))

    @app.route("/healthz")
    def healthz():  # noqa: ANN202
        # Cheap liveness — no DB hit. Useful for "is the dashboard
        # process up at all?" monitoring without load.
        return {"ok": True}

    @app.route("/api/actions")
    def api_actions():  # noqa: ANN202
        return jsonify({"actions": _serialize_catalogue()})

    @app.route("/api/actions/status")
    def api_actions_status():  # noqa: ANN202
        return jsonify({
            "current": _serialize_run(action_worker.current),
            "last": _serialize_run(action_worker.last_completed),
        })

    @app.route("/api/actions/<name>", methods=["POST"])
    def api_actions_start(name: str):  # noqa: ANN202
        spec = ACTIONS.get(name)
        if spec is None:
            return jsonify({
                "error": f"unknown action {name!r}",
                "available": sorted(ACTIONS.keys()),
            }), 404
        params = request.get_json(silent=True) or {}
        try:
            run = action_worker.start(spec, params)
        except ValueError as exc:
            return jsonify({"error": "invalid params", "detail": str(exc)}), 400
        except ActionWorker.BusyError as exc:
            return jsonify({"error": "busy", "detail": str(exc)}), 409
        return jsonify({"started": _serialize_run(run)}), 202

    @app.route("/api/actions/<run_id>/log")
    def api_actions_log(run_id: str):  # noqa: ANN202
        run = _find_run(action_worker, run_id)
        if run is None:
            return jsonify({"error": f"unknown run id {run_id!r}"}), 404
        start_offset = int(request.args.get("offset", "0"))

        def _sse() -> Any:
            # Plain SSE framing: each stdout line → `data: ...\n\n`.
            # Closes when the action finishes or idle timeout fires.
            for line in tail_stream(run, start_offset=start_offset):
                # Split multi-line payloads across SSE messages so
                # receivers don't have to re-split.
                for sub in line.splitlines() or [""]:
                    yield f"data: {sub}\n\n"
            # Emit a terminal marker with the final status so the UI
            # can render "done" state without re-polling.
            final = _serialize_run(run) or {}
            yield (
                f"event: done\n"
                f"data: {final.get('status','unknown')}|{final.get('exit_code','?')}\n\n"
            )

        return Response(
            stream_with_context(_sse()),
            mimetype="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    return app


def run() -> None:
    # Defaults bind to 0.0.0.0 so the dashboard is reachable from
    # other devices on the LAN (phone, second screen) without env
    # tweaking. The app surfaces pipeline telemetry only — no secrets,
    # no PII, no write endpoints — so LAN exposure is the intended
    # threat model for this dev tool. Set DASHBOARD_HOST=127.0.0.1 if
    # you want localhost-only.
    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")  # noqa: S104
    port = int(os.environ.get("DASHBOARD_PORT", "18080"))
    debug = os.environ.get("DASHBOARD_DEBUG", "").lower() in ("1", "true", "yes")
    app = create_app()
    _LOG.info("dashboard_web on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=debug)
