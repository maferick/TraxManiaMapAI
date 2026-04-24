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

    # generate-map — required base_map_id, optional style/difficulty/seed.

    def test_generate_map_requires_base_map_id(self) -> None:
        spec = ACTIONS["generate-map"]
        with pytest.raises(ValueError, match="base_map_id is required"):
            spec.validate_params({})

    def test_generate_map_coerces_string_base_map_id(self) -> None:
        spec = ACTIONS["generate-map"]
        result = spec.validate_params({"base_map_id": "1212"})
        assert result["base_map_id"] == 1212
        assert result["difficulty"] == "medium"   # default
        assert result["random_seed"] == 42        # default
        assert "style_tag_filter" not in result   # default omitted

    def test_generate_map_rejects_nonnumeric_base_map_id(self) -> None:
        spec = ACTIONS["generate-map"]
        with pytest.raises(ValueError, match="integer"):
            spec.validate_params({"base_map_id": "abc"})

    def test_generate_map_rejects_nonpositive_base_map_id(self) -> None:
        spec = ACTIONS["generate-map"]
        with pytest.raises(ValueError, match=">= 1"):
            spec.validate_params({"base_map_id": 0})

    def test_generate_map_accepts_valid_style(self) -> None:
        spec = ACTIONS["generate-map"]
        result = spec.validate_params(
            {"base_map_id": 1, "style_tag_filter": "Tech"},
        )
        assert result["style_tag_filter"] == "Tech"

    def test_generate_map_rejects_unknown_style(self) -> None:
        spec = ACTIONS["generate-map"]
        with pytest.raises(ValueError, match="style_tag_filter"):
            spec.validate_params(
                {"base_map_id": 1, "style_tag_filter": "SpeedDrift"},
            )

    def test_generate_map_accepts_valid_difficulty(self) -> None:
        spec = ACTIONS["generate-map"]
        result = spec.validate_params(
            {"base_map_id": 1, "difficulty": "hard"},
        )
        assert result["difficulty"] == "hard"

    def test_generate_map_rejects_unknown_difficulty(self) -> None:
        spec = ACTIONS["generate-map"]
        with pytest.raises(ValueError, match="difficulty"):
            spec.validate_params(
                {"base_map_id": 1, "difficulty": "legendary"},
            )

    def test_generate_map_coerces_seed(self) -> None:
        spec = ACTIONS["generate-map"]
        result = spec.validate_params(
            {"base_map_id": 1, "random_seed": "7"},
        )
        assert result["random_seed"] == 7

    def test_generate_map_argv_wires_all_params(self) -> None:
        # The CLI invocation must carry every validated param and a
        # predictable output path. Bad argv = silently-ignored UI input.
        spec = ACTIONS["generate-map"]
        validated = spec.validate_params({
            "base_map_id": 1212,
            "style_tag_filter": "Tech",
            "difficulty": "hard",
            "random_seed": 7,
        })
        argv = spec.cli_args(validated)
        assert argv[0] == "generate-map"
        assert "--base-map-id" in argv and "1212" in argv
        assert "--difficulty" in argv and "hard" in argv
        assert "--random-seed" in argv and "7" in argv
        assert "--style-tag-filter" in argv and "Tech" in argv
        # --output path is under reports/generated-maps/ and embeds
        # base map + seed so re-runs don't clobber each other.
        out_idx = argv.index("--output") + 1
        assert argv[out_idx].startswith("reports/generated-maps/base1212-")
        assert argv[out_idx].endswith("-seed7.json")

    def test_generate_map_argv_omits_style_when_unset(self) -> None:
        spec = ACTIONS["generate-map"]
        validated = spec.validate_params({"base_map_id": 1})
        argv = spec.cli_args(validated)
        assert "--style-tag-filter" not in argv


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
        # Uses a test-only ActionSpec whose cli_args returns a `_noop`
        # sentinel. Before PR F the `generate-map` catalogue entry was
        # this sentinel; now it spawns a real subprocess, so we exercise
        # the noop branch explicitly instead of piggy-backing on it.
        w = ActionWorker()
        spec = ActionSpec(
            name="noop-test",
            title="Noop test",
            hint="worker noop branch",
            cli_args=lambda p: ["_noop", "worker-noop-test"],
            validate_params=lambda p: p,
            expected_minutes=0,
        )
        run = w.start(spec, {})
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
        assert any("worker-noop-test" in ln for ln in last.log_tail)


# ---------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------

def _stubbed_worker() -> ActionWorker:
    """ActionWorker whose default runner succeeds immediately without
    spawning a subprocess. Lets us exercise Flask routes for real-argv
    actions (generate-map, ingest-maps-random, train-ai, …) without
    pulling the CLI / DB into unit tests."""
    w = ActionWorker()

    def _stub(argv, run):
        run.append_log(f"[test-stub] {' '.join(argv)}")
        w._finish(run, 0)   # noqa: SLF001 — test-only access

    w._default_runner = _stub   # noqa: SLF001 — test-only override
    return w


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

    def test_post_generate_map_returns_202(self) -> None:
        # generate-map now spawns a real subprocess. To avoid that in a
        # unit test we inject a worker whose runner short-circuits to
        # success without touching subprocess.Popen / the CLI / the DB.
        w = _stubbed_worker()
        _, client = _make_client(worker=w)
        r = client.post(
            "/api/actions/generate-map",
            json={"base_map_id": 1212},
        )
        assert r.status_code == 202
        started = r.get_json()["started"]
        assert started["action"] == "generate-map"
        assert "id" in started

    def test_post_generate_map_requires_base_map_id(self) -> None:
        _, client = _make_client()
        r = client.post("/api/actions/generate-map", json={})
        assert r.status_code == 400
        assert r.get_json()["error"] == "invalid params"

    def test_second_post_while_busy_returns_409(self) -> None:
        # Use a stub spec whose runner blocks until released. We reuse
        # the generate-map catalogue name so the route accepts the POST,
        # but the first "run" is driven by a pass-through validator so
        # the spec's real validator (which now requires base_map_id)
        # doesn't interfere with the blocking setup.
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
            name="generate-map",  # matches catalogue key so the route accepts it
            title=ACTIONS["generate-map"].title,
            hint=ACTIONS["generate-map"].hint,
            cli_args=lambda p: ["_noop", "blocking"],
            validate_params=lambda p: p,
            expected_minutes=0,
        )
        first = w.start(blocking_spec, {}, runner=_blocking)
        assert ready.wait(timeout=2.0)
        _, client = _make_client(worker=w)
        r = client.post(
            "/api/actions/generate-map",
            json={"base_map_id": 1212},
        )
        assert r.status_code == 409
        payload = r.get_json()
        assert payload["error"] == "busy"
        # PR J: the 409 response carries the currently-running action
        # so the UI can attach its SSE log without a second round trip.
        current = payload["current_run"]
        assert current is not None
        assert current["id"] == first.id
        assert current["action"] == "generate-map"
        assert current["status"] == "running"
        release.set()

    def test_log_route_unknown_id_returns_404(self) -> None:
        _, client = _make_client()
        r = client.get("/api/actions/nope/log")
        assert r.status_code == 404

    def test_status_route_reports_last_run(self) -> None:
        w = _stubbed_worker()
        w.start(ACTIONS["generate-map"], {"base_map_id": 1212})
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
