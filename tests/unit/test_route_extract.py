from __future__ import annotations

import numpy as np
import pytest

from src.replay.telemetry import (
    TELEMETRY_SCHEMA_VERSION,
    ReplayTelemetry,
    SampleFrame,
)
from src.route.clusterers import GridClusterer
from src.route.extract import RouteExtractionError, RouteExtractor


def _straight_replay(
    replay_id: str,
    *,
    duration_ms: int = 10_000,
    lateral_offset: float = 0.0,
    sample_rate_hz: int = 50,
    speed_mps: float = 30.0,
) -> ReplayTelemetry:
    period_ms = 1000 // sample_rate_hz
    n = duration_ms // period_ms + 1
    dx = speed_mps * (period_ms / 1000.0)
    samples = tuple(
        SampleFrame(
            time_ms=i * period_ms,
            x=i * dx,
            y=lateral_offset,
            z=0.0,
            vx=speed_mps,
            vy=0.0,
            vz=0.0,
        )
        for i in range(n)
    )
    return ReplayTelemetry(
        schema_version=TELEMETRY_SCHEMA_VERSION,
        source_replay_id=replay_id,
        sample_rate_hz=sample_rate_hz,
        samples=samples,
        finish_time_ms=(n - 1) * period_ms,
    )


def test_extract_on_aligned_replays_produces_centerline() -> None:
    tels = [
        _straight_replay(f"r{i}", duration_ms=10_000, lateral_offset=0.0)
        for i in range(5)
    ]
    ext = RouteExtractor(
        clusterer=GridClusterer(cell_size=1.0),
        n_centerline_points=50,
    )
    result = ext.extract(tels)
    assert len(result.centerline) == 50
    assert result.diagnostics["n_replays"] == 5
    # Replays share the same line; residual mean lateral distance is only
    # from projecting 500 samples onto a 50-vertex polyline (sub-meter).
    assert result.diagnostics["mean_lateral_distance_m"] < 0.5
    assert result.diagnostics["extraction_confidence"] > 0.9


def test_extract_detects_branches_with_split_paths() -> None:
    # 6 replays; the first 3 run at y=0, the other 3 run at y=15.
    # Since our centerline is seeded from the median and our grid cell
    # is 1m, a 15m lateral separation creates distinct cluster cells.
    tels = [
        _straight_replay(f"r{i}", duration_ms=10_000, lateral_offset=0.0)
        for i in range(3)
    ] + [
        _straight_replay(f"r{i}", duration_ms=10_000, lateral_offset=15.0)
        for i in range(3, 6)
    ]
    ext = RouteExtractor(
        clusterer=GridClusterer(cell_size=2.0),
        n_centerline_points=50,
        branch_bin_size_m=20.0,
        branch_min_samples_per_cluster=5,
    )
    result = ext.extract(tels)
    # There should be at least one branch candidate somewhere along s.
    assert len(result.branches) > 0


def test_extract_rejects_empty_input() -> None:
    ext = RouteExtractor(clusterer=GridClusterer())
    with pytest.raises(RouteExtractionError):
        ext.extract([])


def test_seed_selection_is_median_duration() -> None:
    short = _straight_replay("short", duration_ms=5_000)
    median = _straight_replay("median", duration_ms=10_000)
    long = _straight_replay("long", duration_ms=20_000)
    ext = RouteExtractor(clusterer=GridClusterer())
    result = ext.extract([short, median, long])
    assert result.diagnostics["seed_replay_id"] == "median"


def test_segments_from_checkpoints_when_available() -> None:
    tel = _straight_replay("rc", duration_ms=10_000)
    with_checkpoints = ReplayTelemetry(
        schema_version=tel.schema_version,
        source_replay_id=tel.source_replay_id,
        sample_rate_hz=tel.sample_rate_hz,
        samples=tel.samples,
        player_login=tel.player_login,
        finish_time_ms=tel.finish_time_ms,
        checkpoint_sample_indices=(50, 150, 250),
    )
    ext = RouteExtractor(clusterer=GridClusterer(), n_centerline_points=50)
    result = ext.extract([with_checkpoints])
    assert all(s.reason == "checkpoint" for s in result.segments)
    assert len(result.segments) == 3


def test_segments_uniform_fallback_when_no_checkpoints() -> None:
    tel = _straight_replay("rf", duration_ms=10_000)
    ext = RouteExtractor(
        clusterer=GridClusterer(),
        n_centerline_points=50,
        n_segment_boundaries_default=5,
    )
    result = ext.extract([tel])
    assert len(result.segments) == 5
    assert all(s.reason == "uniform" for s in result.segments)
