"""Phase 2 / PR A: action worker + Flask control-endpoint tests.

Exercises the worker's validation + single-action-lock behaviour, plus
the Flask routes with a stubbed worker so tests never spawn subprocesses.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from tools.dashboard_web.actions import (
    ACTIONS,
    ActionRun,
    ActionSpec,
    ActionWorker,
    tail_stream,
)
from tools.dashboard_web.app import create_app


# ---------------------------------------------------------------------
# Action catalogue
# ---------------------------------------------------------------------

class TestActionCatalogue:
    def test_closed_catalogue_members(self) -> None:
        # If someone adds/removes an action, this test forces a review.
        assert sorted(ACTIONS.keys()) == [
            "generate-map",
            "ingest-maps-random",
            "run-pipeline",
            "score-corridors",
            "train-ai",
        ]

    def test_specs_have_required_fields(self) -> None:
        for spec in ACTIONS.values():
            assert spec.name
            assert spec.title
            assert spec.hint
            assert callable(spec.cli_args)
            assert callable(spec.validate_params)
            assert spec.expected_minutes >= 0


# ---------------------------------------------------------------------
# Param validation
# ---------------------------------------------------------------------

class TestParamValidation:
    def test_ingest_maps_random_accepts_int(self) -> None:
        spec = ACTIONS["ingest-maps-random"]
        assert spec.validate_params({"count": 500}) == {"count": 500}

    def test_ingest_maps_random_coerces_numeric_string(self) -> None:
        spec = ACTIONS["ingest-maps-random"]
        assert spec.validate_params({"count": "1000"}) == {"count": 1000}

    def test_ingest_maps_random_rejects_bad_types(self) -> None:
        spec = ACTIONS["ingest-maps-random"]
        with pytest.raises(ValueError, match="integer"):
            spec.validate_params({"count": "not-a-number"})

    def test_ingest_maps_random_enforces_range(self) -> None:
        spec = ACTIONS["ingest-maps-random"]
        with pytest.raises(ValueError, match=r"\[1, 5000\]"):
            spec.validate_params({"count": 0})
        with pytest.raises(ValueError, match=r"\[1, 5000\]"):
            spec.validate_params({"count": 10_000})

    def test_run_pipeline_accepts_optional_snapshot(self) -> None:
        spec = ACTIONS["run-pipeline"]
        # No snapshot = union run
        assert spec.validate_params({}) == {}
        # Valid id
        assert spec.validate_params({"snapshot": "2026-04-scale-1k"}) == {
            "snapshot": "2026-04-scale-1k",
        }

    def test_run_pipeline_rejects_exotic_snapshot_chars(self) -> None:
        spec = ACTIONS["run-pipeline"]
        with pytest.raises(ValueError, match="snapshot id"):
            spec.validate_params({"snapshot": "bad; rm -rf /"})


# ---------------------------------------------------------------------
# ActionWorker state machine
# ---------------------------------------------------------------------

class TestActionWorker:
    def _spec(self, *, sleep: float = 0.0, exit_code: int = 0) -> ActionSpec:
        """Build a test spec whose argv is a simple sentinel. The worker
        respects the _noop prefix so no subprocess actually spawns."""
        return ActionSpec(
            name="test-action",
            title="Test action",
            hint="pytest only",
            cli_args=lambda p: ["_noop", f"sleep={sleep}", f"rc={exit_code}"],
            validate_params=lambda p: p,
            expected_minutes=0,
        )

    def _blocking_runner(self, ready: threading.Event, release: threading.Event):
        def _runner(argv, run):
            run.append_log(f"starting {argv!r}")
            ready.set()
            release.wait(timeout=5.0)
            run.append_log("done")
            run.completed_at = run.started_at  # sentinel
            run.exit_code = 0
            run.status = "success"
        return _runner

    def test_start_returns_running_run(self) -> None:
        w = ActionWorker()
        ready = threading.Event()
        release = threading.Event()
        spec = self._spec()
        run = w.start(spec, {}, runner=self._blocking_runner(ready, release))
        assert ready.wait(timeout=2.0)
        # While the stub blocks, the worker should report busy.
        snap = w.current
        assert snap is not None
        assert snap.status == "running"
        assert snap.action_name == "test-action"
        release.set()

    def test_second_start_rejected_while_busy(self) -> None:
        w = ActionWorker()
        ready = threading.Event()
        release = threading.Event()
        spec = self._spec()
        w.start(spec, {}, runner=self._blocking_runner(ready, release))
        assert ready.wait(timeout=2.0)
        with pytest.raises(ActionWorker.BusyError):
            w.start(spec, {})
        release.set()

    def test_noop_action_marks_success(self) -> None:
        w = ActionWorker()
        run = w.start(ACTIONS["generate-map"], {})
        # Wait up to 2s for the worker thread to finish.
        for _ in range(40):
            if w.current is None:
                break
            time.sleep(0.05)
        assert w.current is None
        last = w.last_completed
        assert last is not None
        assert last.id == run.id
        assert last.status == "success"
        assert last.exit_code == 0
        # The "stub" log line is present.
        assert any("generation stub" in ln for ln in last.log_tail)


# ---------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------

def _make_client(worker=None):
    # State fetcher stubbed out — action endpoints don't need DB.
    from datetime import datetime, timezone
    from tools.dashboard.state import DashboardState

    def _fake_state():
        return DashboardState(collected_at=datetime.now(tz=timezone.utc))
    app = create_app(state_fetcher=_fake_state, worker=worker or ActionWorker())
    app.config.update(TESTING=True)
    return app, app.test_client()


class TestActionRoutes:
    def test_api_actions_lists_catalogue(self) -> None:
        _, client = _make_client()
        r = client.get("/api/actions")
        assert r.status_code == 200
        names = {a["name"] for a in r.get_json()["actions"]}
        assert names == set(ACTIONS.keys())

    def test_post_unknown_action_returns_404(self) -> None:
        _, client = _make_client()
        r = client.post("/api/actions/nonexistent", json={})
        assert r.status_code == 404
        assert "unknown action" in r.get_json()["error"]

    def test_post_invalid_params_returns_400(self) -> None:
        _, client = _make_client()
        r = client.post("/api/actions/ingest-maps-random", json={"count": "foo"})
        assert r.status_code == 400
        assert r.get_json()["error"] == "invalid params"

    def test_post_noop_action_returns_202(self) -> None:
        _, client = _make_client()
        r = client.post("/api/actions/generate-map", json={})
        assert r.status_code == 202
        started = r.get_json()["started"]
        assert started["action"] == "generate-map"
        assert "id" in started

    def test_second_post_while_busy_returns_409(self) -> None:
        # Use a stub spec whose runner blocks until released.
        w = ActionWorker()
        ready = threading.Event()
        release = threading.Event()

        def _blocking(argv, run):
            ready.set()
            release.wait(timeout=5.0)
            run.exit_code = 0
            run.status = "success"
            from datetime import datetime, timezone
            run.completed_at = datetime.now(tz=timezone.utc)

        blocking_spec = ActionSpec(
            name="generate-map",  # reuse catalogue name so the route accepts it
            title=ACTIONS["generate-map"].title,
            hint=ACTIONS["generate-map"].hint,
            cli_args=lambda p: ["_noop", "blocking"],
            validate_params=lambda p: p,
            expected_minutes=0,
        )
        first = w.start(blocking_spec, {}, runner=_blocking)
        assert ready.wait(timeout=2.0)
        _, client = _make_client(worker=w)
        r = client.post("/api/actions/generate-map", json={})
        assert r.status_code == 409
        assert r.get_json()["error"] == "busy"
        release.set()

    def test_log_route_unknown_id_returns_404(self) -> None:
        _, client = _make_client()
        r = client.get("/api/actions/nope/log")
        assert r.status_code == 404

    def test_status_route_reports_last_run(self) -> None:
        w = ActionWorker()
        w.start(ACTIONS["generate-map"], {})
        # Spin until done
        for _ in range(40):
            if w.current is None:
                break
            time.sleep(0.05)
        _, client = _make_client(worker=w)
        payload = client.get("/api/actions/status").get_json()
        assert payload["current"] is None
        assert payload["last"]["status"] == "success"


# ---------------------------------------------------------------------
# tail_stream helper
# ---------------------------------------------------------------------

class TestTailStream:
    def test_replays_from_offset(self) -> None:
        from datetime import datetime, timezone
        run = ActionRun(
            id="t1", action_name="x", title="X",
            started_at=datetime.now(tz=timezone.utc),
            cli_argv=[],
        )
        for i in range(5):
            run.append_log(f"line {i}")
        run.status = "success"
        out = list(tail_stream(run, start_offset=2, idle_timeout_seconds=0.1))
        assert out == ["line 2", "line 3", "line 4"]

    def test_empty_run_exits_cleanly(self) -> None:
        from datetime import datetime, timezone
        run = ActionRun(
            id="t2", action_name="x", title="X",
            started_at=datetime.now(tz=timezone.utc),
            cli_argv=[],
        )
        run.status = "success"
        out = list(tail_stream(run, idle_timeout_seconds=0.1))
        assert out == []
