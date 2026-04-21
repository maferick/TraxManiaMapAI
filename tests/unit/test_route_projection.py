from __future__ import annotations

import numpy as np
import pytest

from src.route.artifact import Centerline, CenterlinePoint
from src.route.projection import (
    build_initial_centerline,
    project_points,
    project_telemetry,
)
from tests.unit._telemetry_builders import make_telemetry


def _straight_centerline() -> Centerline:
    return Centerline(
        (
            CenterlinePoint(s=0.0, x=0.0, y=0.0, z=0.0),
            CenterlinePoint(s=10.0, x=10.0, y=0.0, z=0.0),
            CenterlinePoint(s=20.0, x=20.0, y=0.0, z=0.0),
        )
    )


def test_point_on_centerline_projects_to_itself() -> None:
    cl = _straight_centerline()
    r = project_points(np.array([[5.0, 0.0, 0.0]]), cl)
    assert r.s_values[0] == pytest.approx(5.0)
    assert np.allclose(r.offsets[0], [0.0, 0.0, 0.0])


def test_lateral_offset_preserves_s() -> None:
    cl = _straight_centerline()
    r = project_points(np.array([[5.0, 3.0, 0.0]]), cl)
    assert r.s_values[0] == pytest.approx(5.0)
    # Offset is the lateral +y=3.
    assert np.allclose(r.offsets[0], [0.0, 3.0, 0.0])
    assert r.lateral_distance[0] == pytest.approx(3.0)


def test_projection_clips_beyond_ends() -> None:
    cl = _straight_centerline()
    r = project_points(np.array([[-5.0, 0.0, 0.0], [25.0, 0.0, 0.0]]), cl)
    # Query before start projects to start vertex (s=0).
    assert r.s_values[0] == pytest.approx(0.0)
    # Query after end projects to end vertex (s=20).
    assert r.s_values[1] == pytest.approx(20.0)


def test_rejects_non_3d_points() -> None:
    cl = _straight_centerline()
    with pytest.raises(ValueError, match="M, 3"):
        project_points(np.array([[0.0, 0.0]]), cl)


def test_multiple_points_vectorized() -> None:
    cl = _straight_centerline()
    pts = np.array([[i, 0.0, 0.0] for i in range(0, 21, 2)], dtype=np.float64)
    r = project_points(pts, cl)
    assert np.allclose(r.s_values, np.arange(0, 21, 2, dtype=np.float64))


def test_build_initial_centerline_is_arc_length_uniform() -> None:
    t = make_telemetry(duration_ms=10_000, straight_speed_mps=30.0, sample_rate_hz=50)
    cl = build_initial_centerline(t, resample_n=11)
    assert len(cl) == 11
    # Uniform arc-length increments within tolerance.
    s_vals = np.array([p.s for p in cl.points])
    increments = np.diff(s_vals)
    assert np.allclose(increments, increments[0], rtol=1e-6)


def test_project_telemetry_shape() -> None:
    t = make_telemetry(duration_ms=5_000, sample_rate_hz=50)
    cl = build_initial_centerline(t, resample_n=50)
    r = project_telemetry(t, cl)
    assert r.s_values.shape[0] == len(t.samples)


def test_build_initial_centerline_rejects_zero_length_path() -> None:
    t = make_telemetry(duration_ms=5_000, straight_speed_mps=0.0)
    with pytest.raises(ValueError, match="zero length"):
        build_initial_centerline(t)


def test_build_initial_centerline_rejects_bad_resample_n() -> None:
    t = make_telemetry(duration_ms=1_000)
    with pytest.raises(ValueError, match="resample_n"):
        build_initial_centerline(t, resample_n=1)
