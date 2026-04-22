"""Flask app for the web dashboard.

Reuses :mod:`tools.dashboard.state` — the decision-layer data model
is presentation-agnostic (pure Python, no UI dep). The TUI renders
via Rich markup; this app renders via Jinja templates. Both surface
the same four panels (health, coverage, bottlenecks, freshness) +
the counters panel.

Routes:
- ``GET /``          — HTML dashboard page
- ``GET /api/state`` — JSON snapshot (for programmatic consumers +
                       client-side progressive enhancement)
- ``GET /healthz``   — cheap liveness check (no DB hit)

Auto-refresh is HTML ``<meta http-equiv="refresh">`` — simplest
correct answer; we don't need SSE or websockets for a 10-second
cadence. If we ever want partial DOM updates without full reload,
we add a small fetch+swap on top of ``/api/state``.

Deliberately simple:
- No auth. Bind to localhost by default; set DASHBOARD_HOST for LAN.
- No background job runner; the pipeline stage buttons stay on TUI.
- No database connection pool; each request opens + closes. Fine at
  manual-refresh cadence.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template

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


def create_app(*, state_fetcher: Any = None, refresh_seconds: int = 10) -> Flask:
    """Construct a Flask app. ``state_fetcher`` is an override point
    for tests so they can inject a stub without hitting the DB.
    ``refresh_seconds`` tunes the HTML meta-refresh cadence."""
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    fetch = state_fetcher or _fetch_state_sync
    app.jinja_env.filters["humanize_age"] = _humanize_age_filter
    app.jinja_env.filters["percent"] = _percent_filter

    @app.route("/")
    def dashboard():  # noqa: ANN202
        state = fetch()
        return render_template(
            "dashboard.html",
            state=state,
            refresh_seconds=refresh_seconds,
        )

    @app.route("/api/state")
    def api_state():  # noqa: ANN202
        return jsonify(_state_to_dict(fetch()))

    @app.route("/healthz")
    def healthz():  # noqa: ANN202
        # Cheap liveness — no DB hit. Useful for "is the dashboard
        # process up at all?" monitoring without load.
        return {"ok": True}

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
