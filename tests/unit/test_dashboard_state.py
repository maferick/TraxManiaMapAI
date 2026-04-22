"""Unit tests for the dashboard decision layer.

Exercise the pure parts (thresholds, bottleneck rules, render helpers)
without hitting Textual or the DB.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from tools.dashboard.render import (
    _humanize_age,
    render_bottlenecks,
    render_coverage,
    render_freshness,
    render_health,
)
from tools.dashboard.state import (
    Bottleneck,
    Coverage,
    Health,
    StageFreshness,
    _bucket,
    _compute_bottlenecks,
    _compute_coverage,
    _compute_healths,
)


def _base_counters(**overrides) -> dict[str, int]:
    c = {
        "maps_total": 1000,
        "maps_parsed": 990,
        "maps_failed_permanent": 0,
        "replays_total": 500,
        "replays_clean": 300,
        "replays_rejected": 30,
        "replays_processed": 330,
        "replays_with_breadcrumbs": 500,
        "replays_with_cohort": 0,
        "maps_with_any_replay": 250,
        "maps_with_clean_replays": 240,
        "maps_with_corridors": 200,
        "corridor_maps_with_clean_replays": 80,
        "corridors_total": 800,
        "corridors_top_rank": 200,
        "corridors_with_learned_score": 0,
        "maps_with_time_envelope_label": 75,
    }
    c.update(overrides)
    return c


class TestBucket:
    def test_green_at_or_above_threshold(self) -> None:
        assert _bucket(0.95, green=0.95, yellow=0.80) == "GREEN"
        assert _bucket(1.0, green=0.95, yellow=0.80) == "GREEN"

    def test_yellow_between(self) -> None:
        assert _bucket(0.85, green=0.95, yellow=0.80) == "YELLOW"

    def test_red_below(self) -> None:
        assert _bucket(0.50, green=0.95, yellow=0.80) == "RED"


class TestComputeHealths:
    def test_ingest_green_on_high_parse_rate(self) -> None:
        healths = _compute_healths(_base_counters(maps_parsed=990, maps_total=1000))
        by_name = {h.name: h for h in healths}
        assert by_name["ingest"].status == "GREEN"

    def test_ingest_red_on_low_parse_rate(self) -> None:
        healths = _compute_healths(_base_counters(maps_parsed=500, maps_total=1000))
        by_name = {h.name: h for h in healths}
        assert by_name["ingest"].status == "RED"

    def test_ingest_unknown_when_no_maps(self) -> None:
        healths = _compute_healths(_base_counters(maps_total=0, maps_parsed=0))
        by_name = {h.name: h for h in healths}
        assert by_name["ingest"].status == "UNKNOWN"

    def test_cohorts_red_when_zero_despite_clean(self) -> None:
        # This is the user-flagged blocker: "clean replays but 0 cohorts".
        healths = _compute_healths(_base_counters(
            replays_clean=300, replays_with_cohort=0,
        ))
        by_name = {h.name: h for h in healths}
        assert by_name["cohorts"].status == "RED"
        assert "assign-cohorts" in by_name["cohorts"].detail

    def test_cohorts_green_at_high_coverage(self) -> None:
        healths = _compute_healths(_base_counters(
            replays_clean=100, replays_with_cohort=50,
        ))
        by_name = {h.name: h for h in healths}
        assert by_name["cohorts"].status == "GREEN"

    def test_corridors_green_on_large_corpus(self) -> None:
        healths = _compute_healths(_base_counters(maps_with_corridors=200))
        by_name = {h.name: h for h in healths}
        assert by_name["corridors"].status == "GREEN"

    def test_corridors_red_when_zero(self) -> None:
        healths = _compute_healths(_base_counters(maps_with_corridors=0))
        by_name = {h.name: h for h in healths}
        assert by_name["corridors"].status == "RED"

    def test_learning_red_when_no_scoring(self) -> None:
        healths = _compute_healths(_base_counters(
            corridors_top_rank=200, corridors_with_learned_score=0,
        ))
        by_name = {h.name: h for h in healths}
        assert by_name["learning"].status == "RED"

    def test_learning_green_at_full_coverage(self) -> None:
        healths = _compute_healths(_base_counters(
            corridors_top_rank=200, corridors_with_learned_score=180,
        ))
        by_name = {h.name: h for h in healths}
        assert by_name["learning"].status == "GREEN"


class TestBottlenecks:
    def test_flags_zero_cohort(self) -> None:
        items = _compute_bottlenecks(_base_counters(
            replays_clean=300, replays_with_cohort=0,
        ))
        titles = [b.title for b in items]
        assert any("cohort" in t.lower() for t in titles)
        cohort = next(b for b in items if "cohort" in b.title.lower())
        assert cohort.severity == "RED"

    def test_no_cohort_bottleneck_when_none_clean(self) -> None:
        # Can't assign cohorts with no clean replays; don't nag.
        items = _compute_bottlenecks(_base_counters(
            replays_clean=0, replays_with_cohort=0,
        ))
        assert not any("cohort" in b.title.lower() for b in items)

    def test_flags_missing_learned_score(self) -> None:
        items = _compute_bottlenecks(_base_counters(
            corridors_top_rank=200, corridors_with_learned_score=0,
        ))
        assert any("learned" in b.title.lower() for b in items)

    def test_no_learned_bottleneck_when_no_corridors(self) -> None:
        items = _compute_bottlenecks(_base_counters(
            corridors_top_rank=0, corridors_with_learned_score=0,
        ))
        assert not any("learned" in b.title.lower() for b in items)

    def test_flags_thin_replay_coverage_on_corridor_maps(self) -> None:
        items = _compute_bottlenecks(_base_counters(
            maps_with_corridors=200, corridor_maps_with_clean_replays=40,
        ))
        # 40 / 200 = 20% covered → 80% uncovered > 30% threshold
        assert any("replay coverage thin" in b.title.lower() for b in items)

    def test_no_coverage_bottleneck_when_mostly_covered(self) -> None:
        items = _compute_bottlenecks(_base_counters(
            maps_with_corridors=200, corridor_maps_with_clean_replays=180,
        ))
        assert not any("replay coverage" in b.title.lower() for b in items)

    def test_flags_permanent_parse_failures(self) -> None:
        items = _compute_bottlenecks(_base_counters(maps_failed_permanent=5))
        assert any("permanent" in b.title.lower() for b in items)

    def test_red_items_before_yellow(self) -> None:
        items = _compute_bottlenecks(_base_counters(
            replays_clean=300, replays_with_cohort=0,       # RED
            corridors_top_rank=200, corridors_with_learned_score=0,  # RED
            maps_with_corridors=200, corridor_maps_with_clean_replays=40,  # YELLOW
            maps_failed_permanent=5,                        # YELLOW
        ))
        severities = [b.severity for b in items]
        # All RED before any YELLOW.
        first_yellow = next((i for i, s in enumerate(severities) if s == "YELLOW"), None)
        assert first_yellow is not None
        assert all(s == "RED" for s in severities[:first_yellow])


class TestComputeCoverage:
    def test_carries_all_fields(self) -> None:
        c = _compute_coverage(_base_counters())
        assert c.maps_total == 1000
        assert c.maps_parsed == 990
        assert c.maps_with_replays == 250
        assert c.maps_with_clean_replays == 240
        assert c.maps_with_corridors == 200
        assert c.corridor_maps_with_clean_replays == 80
        assert c.maps_with_time_envelope_label == 75


class TestHumanizeAge:
    def test_seconds(self) -> None:
        now = datetime.now(tz=timezone.utc)
        ts = now - timedelta(seconds=30)
        assert _humanize_age(ts, now=now) == "30s ago"

    def test_minutes(self) -> None:
        now = datetime.now(tz=timezone.utc)
        ts = now - timedelta(minutes=5)
        assert _humanize_age(ts, now=now) == "5m ago"

    def test_hours(self) -> None:
        now = datetime.now(tz=timezone.utc)
        ts = now - timedelta(hours=3)
        assert _humanize_age(ts, now=now) == "3h ago"

    def test_days(self) -> None:
        now = datetime.now(tz=timezone.utc)
        ts = now - timedelta(days=2)
        assert _humanize_age(ts, now=now) == "2d ago"

    def test_none_renders_never(self) -> None:
        assert _humanize_age(None) == "never"


class TestRenderers:
    def test_render_health_contains_status_and_name(self) -> None:
        text = render_health([
            Health("ingest", "GREEN", "parse success 990/1000"),
            Health("cohorts", "RED", "0 assigned"),
        ])
        assert "ingest" in text
        assert "GREEN" in text
        assert "cohorts" in text
        assert "RED" in text

    def test_render_coverage_contains_label_pool_section(self) -> None:
        c = Coverage(
            maps_total=100, maps_parsed=100,
            maps_with_replays=50, maps_with_clean_replays=40,
            maps_with_corridors=30, corridor_maps_with_clean_replays=12,
            maps_with_time_envelope_label=10,
        )
        text = render_coverage(c)
        assert "label pool" in text.lower()
        assert "100" in text

    def test_render_bottlenecks_empty_shows_all_clear(self) -> None:
        text = render_bottlenecks([])
        assert "no blocking" in text.lower()

    def test_render_bottlenecks_contains_titles(self) -> None:
        text = render_bottlenecks([
            Bottleneck("RED", "No cohort-labeled replays", "run assign-cohorts"),
        ])
        assert "No cohort-labeled replays" in text
        assert "assign-cohorts" in text

    def test_render_freshness_shows_ages(self) -> None:
        now = datetime.now(tz=timezone.utc)
        entries = [
            StageFreshness("ingest_maps", now - timedelta(hours=2), "success"),
            StageFreshness("parse_replays", now - timedelta(minutes=5), "partial"),
        ]
        text = render_freshness(entries, now=now)
        assert "ingest_maps" in text
        assert "2h ago" in text
        assert "partial" in text  # non-success status annotated
