"""Route-inference extractor.

Takes a set of cleaned intent-cohort telemetries for a single map and
produces a :class:`RouteExtractionResult`: refined centerline, branch
candidates, segment boundaries, and diagnostics.

Algorithm (scaffold version):

1. Pick a seed replay (closest to the median total duration).
2. Build an initial centerline by resampling the seed in arc-length.
3. Project every telemetry onto the seed centerline.
4. Refine the centerline once: replace each vertex with the mean of
   replay query points whose projected ``s`` falls in a local window
   around that vertex.
5. Re-project telemetries onto the refined centerline.
6. Run the configured :class:`Clusterer` over the combined
   ``(s, offset_x, offset_y, offset_z)`` array.
7. Detect branch candidates by counting cluster labels per s-bin.
8. Emit segment boundaries at the seed replay's checkpoint sample
   indices (falling back to regular intervals).

Scaffold-quality on purpose — the interesting substitutions land
when real telemetry is available. The abstractions (clusterer,
projection, centerline) are the PR 5 deliverable.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from src.replay.telemetry import ReplayTelemetry
from src.route.artifact import (
    BranchCandidate,
    Centerline,
    CenterlinePoint,
    RouteExtractionResult,
    SegmentBoundary,
)
from src.route.clusterers.base import Clusterer
from src.route.projection import (
    build_initial_centerline,
    project_points,
    project_telemetry,
    telemetry_to_xyz,
)


class RouteExtractionError(RuntimeError):
    pass


@dataclass
class RouteExtractor:
    clusterer: Clusterer
    n_centerline_points: int = 200
    refinement_window_m: float = 5.0
    branch_bin_size_m: float = 10.0
    branch_min_samples_per_cluster: int = 3
    n_segment_boundaries_default: int = 4

    def extract(
        self, telemetries: Sequence[ReplayTelemetry]
    ) -> RouteExtractionResult:
        if not telemetries:
            raise RouteExtractionError("at least one telemetry is required")

        seed = self._pick_seed(telemetries)
        centerline = build_initial_centerline(seed, resample_n=self.n_centerline_points)
        combined_xyz = np.concatenate(
            [telemetry_to_xyz(t) for t in telemetries], axis=0
        )
        projected = project_points(combined_xyz, centerline)
        centerline = self._refine_once(centerline, combined_xyz, projected)
        projected = project_points(combined_xyz, centerline)

        cluster_input = np.column_stack([projected.s_values, projected.offsets])
        cluster_result = self.clusterer.fit_predict(cluster_input)

        branches = self._detect_branches(projected.s_values, cluster_result.labels)
        segments = self._segment_boundaries(seed, centerline)

        diagnostics = {
            "seed_replay_id": seed.source_replay_id,
            "n_replays": len(telemetries),
            "total_samples": int(combined_xyz.shape[0]),
            "clusterer_name": self.clusterer.name,
            "clusterer_version": self.clusterer.version,
            "n_clusters": int(cluster_result.n_clusters),
            "has_noise": bool(cluster_result.has_noise),
            "centerline_length_m": float(centerline.length_m),
            "mean_lateral_distance_m": float(np.mean(projected.lateral_distance)),
            "extraction_confidence": self._confidence(projected.lateral_distance),
        }
        return RouteExtractionResult(
            centerline=centerline,
            branches=tuple(branches),
            segments=tuple(segments),
            diagnostics=diagnostics,
        )

    def _pick_seed(self, telemetries: Sequence[ReplayTelemetry]) -> ReplayTelemetry:
        durations = np.array([t.duration_ms for t in telemetries])
        median = float(np.median(durations))
        best_idx = int(np.argmin(np.abs(durations - median)))
        return telemetries[best_idx]

    def _refine_once(
        self,
        centerline: Centerline,
        all_points_xyz: np.ndarray,
        projected,
    ) -> Centerline:
        s_values = projected.s_values
        delta = self.refinement_window_m
        new_points: list[CenterlinePoint] = []
        for p in centerline.points:
            mask = np.abs(s_values - p.s) <= delta
            if mask.any():
                mean_xyz = np.mean(all_points_xyz[mask], axis=0)
                new_points.append(
                    CenterlinePoint(
                        s=p.s, x=float(mean_xyz[0]), y=float(mean_xyz[1]), z=float(mean_xyz[2])
                    )
                )
            else:
                new_points.append(p)
        return Centerline(tuple(new_points))

    def _detect_branches(
        self, s_values: np.ndarray, labels: np.ndarray
    ) -> list[BranchCandidate]:
        if s_values.size == 0:
            return []
        s_min = float(s_values.min())
        s_max = float(s_values.max())
        bin_size = self.branch_bin_size_m
        n_bins = max(1, int(np.ceil((s_max - s_min) / bin_size)))

        out: list[BranchCandidate] = []
        for b in range(n_bins):
            lo = s_min + b * bin_size
            hi = s_min + (b + 1) * bin_size
            bin_mask = (s_values >= lo) & (s_values < hi)
            if not bin_mask.any():
                continue
            bin_labels = labels[bin_mask]
            counts = Counter(int(x) for x in bin_labels.tolist() if x >= 0)
            if not counts:
                continue
            significant = {
                lab: n for lab, n in counts.items() if n >= self.branch_min_samples_per_cluster
            }
            if len(significant) <= 1:
                continue
            sorted_items = sorted(significant.items(), key=lambda kv: kv[1], reverse=True)
            primary_count = sorted_items[0][1]
            alt_count = sum(n for _, n in sorted_items[1:])
            out.append(
                BranchCandidate(
                    s=(lo + hi) / 2.0,
                    cluster_count=len(significant),
                    replays_in_primary=primary_count,
                    replays_in_alternates=alt_count,
                    evidence={
                        "bin_lo": lo,
                        "bin_hi": hi,
                        "cluster_sizes": dict(sorted_items),
                    },
                )
            )
        return out

    def _segment_boundaries(
        self, seed: ReplayTelemetry, centerline: Centerline
    ) -> list[SegmentBoundary]:
        if seed.checkpoint_sample_indices:
            return self._boundaries_from_checkpoints(seed, centerline)
        return self._boundaries_uniform(centerline)

    def _boundaries_from_checkpoints(
        self, seed: ReplayTelemetry, centerline: Centerline
    ) -> list[SegmentBoundary]:
        checkpoint_xyz = np.array(
            [
                [seed.samples[i].x, seed.samples[i].y, seed.samples[i].z]
                for i in seed.checkpoint_sample_indices
            ],
            dtype=np.float64,
        )
        projected = project_points(checkpoint_xyz, centerline)
        return [
            SegmentBoundary(
                s=float(projected.s_values[k]),
                reason="checkpoint",
                evidence={"source_sample_index": int(seed.checkpoint_sample_indices[k])},
            )
            for k in range(projected.count)
        ]

    def _boundaries_uniform(self, centerline: Centerline) -> list[SegmentBoundary]:
        length = centerline.length_m
        if length <= 0:
            return []
        n = self.n_segment_boundaries_default
        step = length / (n + 1)
        return [
            SegmentBoundary(s=float((k + 1) * step), reason="uniform", evidence={"k": k})
            for k in range(n)
        ]

    def _confidence(self, lateral_distance: np.ndarray) -> float:
        if lateral_distance.size == 0:
            return 0.0
        # Normalized inverse of mean lateral distance; bounded to [0, 1].
        # 0 m mean -> 1.0 (perfect fit); 5 m mean -> ~0.17.
        mean = float(np.mean(lateral_distance))
        return float(1.0 / (1.0 + mean))
