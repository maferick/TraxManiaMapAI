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
    Health,
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
