"""Project replay samples onto a centerline.

For each sample point, we find the closest point on the centerline
polyline (foot of perpendicular, clipped to segment) and return its
arc-length ``s`` plus the 3D offset vector. This is the foundational
operation for centerline refinement and branch detection.

Complexity
----------
Naive: O(M · N) where M = samples and N = centerline vertices. Fine
for scaffold scale (~1k centerline × ~10k samples). A spatial index
is a later-PR optimization.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.replay.telemetry import ReplayTelemetry
from src.route.artifact import Centerline, CenterlinePoint


@dataclass(frozen=True)
class ProjectedSamples:
    s_values: np.ndarray          # (M,) arc-length in meters
    offsets: np.ndarray            # (M, 3) query minus projected foot
    segment_indices: np.ndarray   # (M,) which centerline segment each sample projected onto

    @property
    def count(self) -> int:
        return int(self.s_values.shape[0])

    @property
    def lateral_distance(self) -> np.ndarray:
        return np.linalg.norm(self.offsets, axis=1)


def _centerline_to_arrays(centerline: Centerline) -> tuple[np.ndarray, np.ndarray]:
    xyz = np.array([[p.x, p.y, p.z] for p in centerline.points], dtype=np.float64)
    s = np.array([p.s for p in centerline.points], dtype=np.float64)
    return xyz, s


def project_points(points: np.ndarray, centerline: Centerline) -> ProjectedSamples:
    """Project ``(M, 3)`` points onto the centerline polyline.

    Returns per-sample arc-length, 3D offset, and the segment index.
    """
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError(f"points must have shape (M, 3), got {points.shape}")

    xyz, s_coords = _centerline_to_arrays(centerline)
    starts = xyz[:-1]                                   # (N-1, 3)
    ends = xyz[1:]                                      # (N-1, 3)
    seg_vec = ends - starts                             # (N-1, 3)
    seg_len_sq = np.einsum("ij,ij->i", seg_vec, seg_vec)  # (N-1,)
    # Guard against zero-length segments.
    seg_len_sq_safe = np.where(seg_len_sq == 0.0, 1.0, seg_len_sq)

    # Broadcast: queries (M, 1, 3) against segment starts (1, N-1, 3)
    diff = points[:, None, :] - starts[None, :, :]      # (M, N-1, 3)
    dot = np.einsum("mnc,nc->mn", diff, seg_vec)        # (M, N-1)
    t = dot / seg_len_sq_safe[None, :]                  # (M, N-1)
    t_clipped = np.clip(t, 0.0, 1.0)

    foot = starts[None, :, :] + t_clipped[:, :, None] * seg_vec[None, :, :]  # (M, N-1, 3)
    delta = points[:, None, :] - foot                    # (M, N-1, 3)
    dist_sq = np.einsum("mnc,mnc->mn", delta, delta)    # (M, N-1)

    best = np.argmin(dist_sq, axis=1)                   # (M,)
    rows = np.arange(points.shape[0])
    s_start = s_coords[:-1][best]
    s_end = s_coords[1:][best]
    t_best = t_clipped[rows, best]
    s_values = s_start + t_best * (s_end - s_start)
    offsets = points - foot[rows, best]

    return ProjectedSamples(
        s_values=s_values,
        offsets=offsets,
        segment_indices=best.astype(np.int64),
    )


def telemetry_to_xyz(telemetry: ReplayTelemetry) -> np.ndarray:
    return np.array(
        [[s.x, s.y, s.z] for s in telemetry.samples], dtype=np.float64
    )


def project_telemetry(
    telemetry: ReplayTelemetry, centerline: Centerline
) -> ProjectedSamples:
    return project_points(telemetry_to_xyz(telemetry), centerline)


def build_initial_centerline(
    telemetry: ReplayTelemetry, *, resample_n: int = 200
) -> Centerline:
    """Seed a centerline by evenly resampling a replay in arc-length.

    The seed is refined by ``RouteExtractor``. The resample is uniform
    in arc-length, not in time — this keeps detail in corners even if
    the player slowed down.
    """
    if resample_n < 2:
        raise ValueError("resample_n must be >= 2")
    xyz = telemetry_to_xyz(telemetry)
    if xyz.shape[0] < 2:
        raise ValueError("telemetry must have at least 2 samples")

    segment_vec = np.diff(xyz, axis=0)
    segment_len = np.linalg.norm(segment_vec, axis=1)
    cum_s = np.concatenate([[0.0], np.cumsum(segment_len)])
    total_len = cum_s[-1]
    if total_len <= 0.0:
        raise ValueError("telemetry path has zero length")

    target_s = np.linspace(0.0, total_len, resample_n)
    points: list[CenterlinePoint] = []
    j = 0
    for target in target_s:
        while j < len(cum_s) - 2 and cum_s[j + 1] < target:
            j += 1
        span = cum_s[j + 1] - cum_s[j]
        u = 0.0 if span == 0 else (target - cum_s[j]) / span
        interp = xyz[j] + u * (xyz[j + 1] - xyz[j])
        points.append(
            CenterlinePoint(s=float(target), x=float(interp[0]), y=float(interp[1]), z=float(interp[2]))
        )
    return Centerline(tuple(points))
