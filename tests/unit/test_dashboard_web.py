"""Smoke tests for the Flask web dashboard.

Use ``create_app`` with a stub ``state_fetcher`` so the tests don't
hit the DB. The pure data + render modules already have their own
tests; this file only exercises the Flask wiring (routes exist,
return the expected content types, don't blow up on error states).
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from tools.dashboard.state import (
    Bottleneck,
    Coverage,
    DashboardState,
    DiversityState,
    Health,
    LearningState,
    NextAction,
    StageFreshness,
)
from tools.dashboard_web.app import create_app


def _mk_state(*, error: str | None = None) -> DashboardState:
    if error is not None:
        return DashboardState(
            collected_at=datetime.now(tz=timezone.utc),
            error=error,
        )
    now = datetime.now(tz=timezone.utc)
    return DashboardState(
        collected_at=now,
        healths=[
            Health("ingest", "GREEN", "parse success 990/1000"),
            Health("cohorts", "RED", "0 cohort-labeled replays"),
        ],
        coverage=Coverage(
            maps_total=1000, maps_parsed=990,
            maps_with_replays=250, maps_with_clean_replays=240,
            maps_with_corridors=200, corridor_maps_with_clean_replays=80,
            maps_with_time_envelope_label=75,
        ),
        bottlenecks=[
            Bottleneck("RED", "No cohort-labeled replays", "run assign-cohorts"),
        ],
        freshness=[
            StageFreshness("ingest_maps", now - timedelta(hours=2), "success"),
            StageFreshness("replay_clean", now - timedelta(minutes=5), "partial"),
        ],
        counters={
            "replays_total": 756,
            "replays_with_breadcrumbs": 756,
            "corridors_total": 898,
            "corridors_top_rank": 252,
            "corridors_with_learned_score": 252,
        },
        learning=LearningState(
            scheme_tag="time_envelope_v2_weighted@0.1.0",
            model_hash_short="12cc0e58c19a",
            scored_corridors=898,
            pred_min=0.0255, pred_median=0.5549,
            pred_max=0.7182, pred_mean=0.5452,
            pred_stdev=0.1050,
            heuristic_stdev=0.1731,
            stdev_ratio=0.60,
            status="GREEN",
        ),
        diversity=DiversityState(
            intervals_compared=124,
            heuristic_diversity_median=0.5795,
            learned_diversity_median=0.5401,
            delta_median=-0.0394,
            delta_mean=-0.0038,
            status="GREEN",
            reason="learned and heuristic diversity within tolerance",
        ),
        next_actions=[
            NextAction(
                priority=1,
                title="Assign cohorts to clean replays",
                reason="728 clean replays unlabeled",
                command="python -m src.cli assign-cohorts",
            ),
        ],
    )


@pytest.fixture()
def client():
    app = create_app(state_fetcher=lambda: _mk_state())
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture()
def client_error():
    app = create_app(state_fetcher=lambda: _mk_state(error="db down"))
    app.config.update(TESTING=True)
    return app.test_client()


class TestRoutes:
    def test_dashboard_returns_200_html(self, client) -> None:
        r = client.get("/")
        assert r.status_code == 200
        assert r.content_type.startswith("text/html")

    def test_dashboard_renders_health_rows(self, client) -> None:
        r = client.get("/")
        body = r.data.decode()
        assert "ingest" in body
        assert "GREEN" in body
        assert "cohorts" in body
        assert "RED" in body

    def test_dashboard_renders_bottleneck_detail(self, client) -> None:
        body = client.get("/").data.decode()
        assert "No cohort-labeled replays" in body
        assert "assign-cohorts" in body

    def test_dashboard_renders_coverage_fractions(self, client) -> None:
        body = client.get("/").data.decode()
        # 80 / 200 = 40%
        assert "40%" in body
        assert "80 / 200" in body

    def test_dashboard_renders_freshness_with_partial_status(self, client) -> None:
        body = client.get("/").data.decode()
        assert "2h ago" in body
        assert "replay_clean" in body
        assert "partial" in body

    def test_dashboard_renders_learning_panel(self, client) -> None:
        body = client.get("/").data.decode()
        assert "Learning state" in body
        assert "time_envelope_v2_weighted" in body
        assert "12cc0e58c19a" in body        # model hash short
        assert "0.60" in body                # stdev ratio

    def test_dashboard_renders_diversity_panel(self, client) -> None:
        body = client.get("/").data.decode()
        assert "Diversity watchdog" in body
        assert "within tolerance" in body
        # delta formatted with sign
        assert "-0.0394" in body or "−0.0394" in body

    def test_dashboard_renders_next_action(self, client) -> None:
        body = client.get("/").data.decode()
        assert "Next best action" in body
        assert "Assign cohorts to clean replays" in body
        assert "python -m src.cli assign-cohorts" in body

    def test_dashboard_renders_error_panel(self, client_error) -> None:
        r = client_error.get("/")
        assert r.status_code == 200
        body = r.data.decode()
        assert "Collection error" in body
        assert "db down" in body

    def test_api_state_returns_json(self, client) -> None:
        r = client.get("/api/state")
        assert r.status_code == 200
        assert r.content_type.startswith("application/json")
        payload = r.get_json()
        assert payload["error"] is None
        assert payload["counters"]["replays_total"] == 756
        assert len(payload["healths"]) == 2
        # Datetimes serialized as ISO strings
        assert isinstance(payload["collected_at"], str)
        assert "T" in payload["collected_at"]
        # Nested datetime in freshness also serialized
        assert isinstance(payload["freshness"][0]["completed_at"], str)
        # A5 fields also in JSON
        assert payload["learning"]["scheme_tag"] == "time_envelope_v2_weighted@0.1.0"
        assert payload["diversity"]["status"] == "GREEN"
        assert payload["next_actions"][0]["title"] == "Assign cohorts to clean replays"

    def test_api_state_on_error_still_json(self, client_error) -> None:
        r = client_error.get("/api/state")
        assert r.status_code == 200
        payload = r.get_json()
        assert payload["error"] == "db down"

    def test_healthz_cheap_liveness(self, client) -> None:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.get_json() == {"ok": True}

    def test_static_css_served(self, client) -> None:
        r = client.get("/static/dashboard.css")
        assert r.status_code == 200
        assert "panel" in r.data.decode()


class TestAppFactory:
    def test_refresh_seconds_propagated(self) -> None:
        app = create_app(
            state_fetcher=lambda: _mk_state(),
            refresh_seconds=30,
        )
        client = app.test_client()
        body = client.get("/").data.decode()
        assert 'content="30"' in body

    def test_registers_jinja_filters(self) -> None:
        app = create_app(state_fetcher=lambda: _mk_state())
        assert "humanize_age" in app.jinja_env.filters
        assert "percent" in app.jinja_env.filters


# ---------------------------------------------------------------------
# PR G — generated-results inspection panel
# ---------------------------------------------------------------------

import json
from pathlib import Path

from tools.dashboard_web import app as app_module


def _write_artifact(
    root: Path,
    filename: str,
    *,
    route_verified: bool = True,
    base_map_id: int = 1212,
    ai_confidence: float | None = 0.68,
    reject_reason: str | None = None,
    run_id: str = "abcdef0123456789",
    intervals: int = 3,
    generated_at: str = "2026-04-23T12:00:00+00:00",
    extra: dict | None = None,
    schema_version: str = "generation-v0",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": schema_version,
        "run_id": run_id,
        "generated_at": generated_at,
        "inputs": {
            "base_map_id": base_map_id,
            "base_map_source_id": "src-42",
            "style_tag_filter": None,
            "difficulty": "medium",
            "random_seed": 42,
        },
        "finishability": {
            "route_verified": route_verified,
            "estimated_time_ms": 11732 if route_verified else None,
            "ai_confidence": ai_confidence,
            "reject_reason": reject_reason,
            "gate_version": "finishability-v0",
        },
        "route": {
            "intervals": [{}] * intervals,
            "cells_total": intervals * 3,
            "corridors_used": [{}] * intervals,
        },
    }
    if extra is not None:
        payload.update(extra)
    path = root / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestSummarizeArtifact:
    def test_parses_all_fields(self, tmp_path: Path) -> None:
        path = _write_artifact(tmp_path, "base1212-a.json")
        summary = app_module._summarize_generated_artifact(path)
        assert summary is not None
        assert summary["filename"] == "base1212-a.json"
        assert summary["run_id"] == "abcdef0123456789"
        assert summary["base_map_id"] == 1212
        assert summary["route_verified"] is True
        assert summary["reject_reason"] is None
        assert summary["ai_confidence"] == 0.68
        assert summary["estimated_time_ms"] == 11732
        assert summary["interval_count"] == 3

    def test_rejects_non_v0_schema(self, tmp_path: Path) -> None:
        # Future majors (v1+) or unrelated schemas → filtered out.
        path = _write_artifact(
            tmp_path, "wrong.json", schema_version="generation-v1",
        )
        assert app_module._summarize_generated_artifact(path) is None

    def test_accepts_v0_1_stripped_artifact(self, tmp_path: Path) -> None:
        # Level-2 stripped artifacts use schema_version="generation-v0.1".
        # The dashboard must surface them — before this fix the whole
        # v0.x family was silently dropped.
        path = _write_artifact(
            tmp_path, "strip.json",
            schema_version="generation-v0.1",
            extra={
                "map": {
                    "waypoint_order_style": "linked",
                    "interval_count": 3,
                    "blocks": [],
                    "checkpoints": [],
                    "stripped": True,
                    "strip_policy": "halo_axis_1",
                    "kept_block_count": 33,
                    "base_block_count": 541,
                },
            },
        )
        summary = app_module._summarize_generated_artifact(path)
        assert summary is not None
        assert summary["schema_version"] == "generation-v0.1"
        assert summary["stripped"] is True
        assert summary["kept_block_count"] == 33
        assert summary["base_block_count"] == 541

    def test_rejects_malformed_json(self, tmp_path: Path) -> None:
        path = tmp_path / "junk.json"
        path.write_text("{not json", encoding="utf-8")
        assert app_module._summarize_generated_artifact(path) is None

    def test_handles_reject_artifact(self, tmp_path: Path) -> None:
        path = _write_artifact(
            tmp_path, "rejected.json",
            route_verified=False,
            ai_confidence=None,
            reject_reason="plain_cp_not_supported_v0",
            intervals=0,
        )
        summary = app_module._summarize_generated_artifact(path)
        assert summary is not None
        assert summary["route_verified"] is False
        assert summary["reject_reason"] == "plain_cp_not_supported_v0"
        assert summary["ai_confidence"] is None
        assert summary["interval_count"] == 0


class TestListGenerated:
    def test_most_recent_first(self, tmp_path: Path) -> None:
        import os, time
        a = _write_artifact(tmp_path, "a.json", run_id="aaaaaaaaaaaaaaaa")
        time.sleep(0.01)
        b = _write_artifact(tmp_path, "b.json", run_id="bbbbbbbbbbbbbbbb")
        # Nudge mtimes explicitly so ordering is deterministic on fast FS
        os.utime(a, (1000, 1000))
        os.utime(b, (2000, 2000))
        items = app_module._list_generated_artifacts(tmp_path)
        assert [s["run_id"] for s in items] == [
            "bbbbbbbbbbbbbbbb", "aaaaaaaaaaaaaaaa",
        ]

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        assert app_module._list_generated_artifacts(tmp_path) == []

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert app_module._list_generated_artifacts(tmp_path / "nope") == []

    def test_skips_malformed_files(self, tmp_path: Path) -> None:
        _write_artifact(tmp_path, "good.json")
        (tmp_path / "junk.json").write_text("{bad", encoding="utf-8")
        items = app_module._list_generated_artifacts(tmp_path)
        assert [s["filename"] for s in items] == ["good.json"]

    def test_respects_limit(self, tmp_path: Path) -> None:
        for i in range(5):
            _write_artifact(tmp_path, f"r{i}.json")
        items = app_module._list_generated_artifacts(tmp_path, limit=3)
        assert len(items) == 3


class TestGeneratedApiRoutes:
    def _client_with_dir(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(app_module, "_GENERATED_MAPS_DIR", tmp_path)
        app = create_app(state_fetcher=lambda: _mk_state())
        app.config.update(TESTING=True)
        return app.test_client()

    def test_list_empty(self, tmp_path: Path, monkeypatch) -> None:
        client = self._client_with_dir(tmp_path, monkeypatch)
        r = client.get("/api/generated-maps")
        assert r.status_code == 200
        payload = r.get_json()
        assert payload == {
            "latest": None, "recent": [],
            "total_returned": 0, "cap": 20,
        }

    def test_list_with_artifacts(self, tmp_path: Path, monkeypatch) -> None:
        _write_artifact(tmp_path, "ok.json")
        client = self._client_with_dir(tmp_path, monkeypatch)
        payload = client.get("/api/generated-maps").get_json()
        assert payload["latest"]["filename"] == "ok.json"
        assert len(payload["recent"]) == 1

    def test_download_serves_json(self, tmp_path: Path, monkeypatch) -> None:
        _write_artifact(tmp_path, "dl.json", run_id="0123456789abcdef")
        client = self._client_with_dir(tmp_path, monkeypatch)
        r = client.get("/api/generated-maps/dl.json")
        assert r.status_code == 200
        assert r.mimetype == "application/json"
        body = json.loads(r.data)
        assert body["run_id"] == "0123456789abcdef"

    def test_download_rejects_traversal(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        client = self._client_with_dir(tmp_path, monkeypatch)
        # Path traversal + unsafe-name attempts all 404, not 200-outside-dir.
        for bad in ("../etc/passwd", "..%2fetc", "weird name.json", "no-ext"):
            r = client.get(f"/api/generated-maps/{bad}")
            assert r.status_code == 404, bad

    def test_download_missing_returns_404(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        client = self._client_with_dir(tmp_path, monkeypatch)
        r = client.get("/api/generated-maps/notthere.json")
        assert r.status_code == 404


class TestDashboardRendersLatest:
    def test_panel_visible_when_artifact_exists(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        _write_artifact(tmp_path, "base1212-x.json",
                        run_id="deadbeefdeadbeef", base_map_id=1212)
        monkeypatch.setattr(app_module, "_GENERATED_MAPS_DIR", tmp_path)
        app = create_app(state_fetcher=lambda: _mk_state())
        body = app.test_client().get("/").data.decode()
        assert 'id="generated-results"' in body
        assert "base #1212" in body
        assert "deadbeefdeadbeef" in body
        assert "badge-verified" in body
        assert "base1212-x.json" in body

    def test_panel_empty_state(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(app_module, "_GENERATED_MAPS_DIR", tmp_path)
        app = create_app(state_fetcher=lambda: _mk_state())
        body = app.test_client().get("/").data.decode()
        assert "No generated-map artifacts yet" in body


# ---------------------------------------------------------------------
# PR I — GBX emit + download
# ---------------------------------------------------------------------

class TestGbxEmitRoute:
    def _client(self, tmp_path: Path, monkeypatch):
        maps_dir = tmp_path / "maps"
        gbx_dir = tmp_path / "gbx"
        maps_dir.mkdir()
        gbx_dir.mkdir()
        monkeypatch.setattr(app_module, "_GENERATED_MAPS_DIR", maps_dir)
        monkeypatch.setattr(app_module, "_GENERATED_GBX_DIR", gbx_dir)
        app = create_app(state_fetcher=lambda: _mk_state())
        app.config.update(TESTING=True)
        return app.test_client(), maps_dir, gbx_dir

    def _stub_emit_success(
        self, tmp_gbx_dir: Path, monkeypatch, *, filename="out.Map.Gbx",
    ):
        """Monkeypatch everything the emit route imports so it runs
        without a real DB or C# binary. Returns the (empty) .Map.Gbx
        file we faked into existence so the test can verify that the
        follow-up download route serves it."""
        # 1. src.utils.config.load_config → minimal config dict
        from src.utils import config as cfg_mod
        monkeypatch.setattr(cfg_mod, "load_config", lambda _=None: {
            "parsers": {"gbx": {
                "executable": "/bin/true",  # must exist for the wrapper-missing guard
                "timeout_seconds": 30.0,
                "parser_version": "0.1.0",
            }},
        })
        # 2. src.storage.mariadb.open_connection → mock returning a close()-able
        from unittest.mock import MagicMock
        from src.storage import mariadb
        monkeypatch.setattr(mariadb, "open_connection", lambda _c: MagicMock())
        # 3. src.parsers.SubprocessParser → no-op constructor
        import src.parsers as parsers_pkg
        monkeypatch.setattr(
            parsers_pkg, "SubprocessParser",
            lambda **kw: MagicMock(),
        )
        # 4. emit_gbx_from_artifact_file → fake GbxEmitResult
        from src.generation import gbx_writer
        fake_gbx = tmp_gbx_dir / filename
        fake_gbx.write_bytes(b"fake gbx body")
        result = gbx_writer.GbxEmitResult(
            output_path=fake_gbx,
            new_map_uid="abcDEF0123456789abcDEFX",
            new_map_name="Smoke Test Title",
            base_path=Path("/tmp/base.Map.Gbx"),
            block_count=541,
            baked_block_count=3250,
            subprocess_duration_ms=280,
        )
        monkeypatch.setattr(
            gbx_writer, "emit_gbx_from_artifact_file",
            lambda conn, artifact_path, parser, output_dir: result,
        )
        return fake_gbx

    def _write_artifact(self, maps_dir: Path, name="base1212-test.json"):
        (maps_dir / name).write_text(json.dumps({
            "schema_version": "generation-v0",
            "run_id": "testrunid12345",
            "inputs": {"base_map_id": 1212, "random_seed": 42,
                       "difficulty": "medium", "style_tag_filter": None,
                       "base_map_source_id": "x"},
            "finishability": {
                "route_verified": True, "estimated_time_ms": 11732,
                "ai_confidence": 0.68, "reject_reason": None,
                "gate_version": "finishability-v0",
            },
            "route": {"intervals": [], "cells_total": 0, "corridors_used": []},
        }), encoding="utf-8")
        return name

    def test_happy_path_returns_download_url(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        client, maps_dir, gbx_dir = self._client(tmp_path, monkeypatch)
        self._stub_emit_success(gbx_dir, monkeypatch, filename="out.Map.Gbx")
        name = self._write_artifact(maps_dir)
        r = client.post(f"/api/generated-maps/{name}/emit-gbx")
        assert r.status_code == 200
        payload = r.get_json()
        assert payload["artifact"] == name
        assert payload["gbx_filename"] == "out.Map.Gbx"
        assert payload["download_url"] == "/api/generated-gbx/out.Map.Gbx"
        assert payload["block_count"] == 541
        assert payload["new_map_uid"] == "abcDEF0123456789abcDEFX"

    def test_unknown_artifact_returns_404(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        client, _, _ = self._client(tmp_path, monkeypatch)
        r = client.post("/api/generated-maps/nope.json/emit-gbx")
        assert r.status_code == 404

    def test_bad_artifact_name_returns_404(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        client, _, _ = self._client(tmp_path, monkeypatch)
        for bad in ("../secret", "weird name.json", "no-ext"):
            r = client.post(f"/api/generated-maps/{bad}/emit-gbx")
            assert r.status_code == 404, bad

    def test_wrapper_missing_returns_500(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        client, maps_dir, _ = self._client(tmp_path, monkeypatch)
        from src.utils import config as cfg_mod
        monkeypatch.setattr(cfg_mod, "load_config", lambda _=None: {
            "parsers": {"gbx": {
                "executable": "/definitely/does/not/exist/gbxwrap",
                "timeout_seconds": 30.0, "parser_version": "0.1.0",
            }},
        })
        self._write_artifact(maps_dir)
        r = client.post("/api/generated-maps/base1212-test.json/emit-gbx")
        assert r.status_code == 500
        payload = r.get_json()
        assert payload["error"] == "wrapper binary missing"

    def test_emit_error_surfaces_500(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        client, maps_dir, gbx_dir = self._client(tmp_path, monkeypatch)
        from src.utils import config as cfg_mod
        from src.storage import mariadb
        from unittest.mock import MagicMock
        import src.parsers as parsers_pkg
        from src.generation import gbx_writer
        monkeypatch.setattr(cfg_mod, "load_config", lambda _=None: {
            "parsers": {"gbx": {
                "executable": "/bin/true", "timeout_seconds": 30.0,
                "parser_version": "0.1.0",
            }},
        })
        monkeypatch.setattr(mariadb, "open_connection", lambda _c: MagicMock())
        monkeypatch.setattr(
            parsers_pkg, "SubprocessParser", lambda **kw: MagicMock(),
        )
        def _raise(conn, artifact_path, parser, output_dir):
            raise gbx_writer.GbxEmitError("base map gone from disk")
        monkeypatch.setattr(gbx_writer, "emit_gbx_from_artifact_file", _raise)
        self._write_artifact(maps_dir)
        r = client.post("/api/generated-maps/base1212-test.json/emit-gbx")
        assert r.status_code == 500
        payload = r.get_json()
        assert payload["error"] == "emit failed"
        assert "base map gone" in payload["detail"]


class TestGbxDownloadRoute:
    def _client(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(app_module, "_GENERATED_GBX_DIR", tmp_path)
        app = create_app(state_fetcher=lambda: _mk_state())
        app.config.update(TESTING=True)
        return app.test_client()

    def test_serves_gbx(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "base1212-abc.Map.Gbx").write_bytes(b"binary-content")
        client = self._client(tmp_path, monkeypatch)
        r = client.get("/api/generated-gbx/base1212-abc.Map.Gbx")
        assert r.status_code == 200
        assert r.data == b"binary-content"
        # as_attachment → Content-Disposition: attachment
        assert "attachment" in r.headers.get("Content-Disposition", "")

    def test_rejects_non_gbx_extension(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        client = self._client(tmp_path, monkeypatch)
        for bad in ("foo.json", "foo.Map.Gbx.tmp", "../etc/passwd"):
            r = client.get(f"/api/generated-gbx/{bad}")
            assert r.status_code == 404, bad

    def test_missing_file_returns_404(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        client = self._client(tmp_path, monkeypatch)
        r = client.get("/api/generated-gbx/base9-notthere.Map.Gbx")
        assert r.status_code == 404
