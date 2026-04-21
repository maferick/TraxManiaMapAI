"""On-disk route-artifact types.

A route artifact is a centerline + branch candidates + segment
boundaries stored as JSON on the filesystem. The DB row in
``route_artifacts`` (migration 007) references this file by
``centerline_path`` + ``centerline_hash``.

Provenance fields (``clustering_method``, ``clustering_params``,
``replay_cohort``, ``extraction_confidence``) live on the DB row; the
on-disk artifact stores only the geometric result.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

ROUTE_ARTIFACT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CenterlinePoint:
    s: float          # cumulative arc-length from start, meters
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class Centerline:
    points: tuple[CenterlinePoint, ...]

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("centerline must have at least 2 points")
        prev_s = self.points[0].s
        for i, p in enumerate(self.points[1:], start=1):
            if p.s < prev_s:
                raise ValueError(
                    f"centerline s must be non-decreasing: points[{i}].s={p.s} < {prev_s}"
                )
            prev_s = p.s

    @property
    def length_m(self) -> float:
        return self.points[-1].s - self.points[0].s

    def __len__(self) -> int:
        return len(self.points)


@dataclass(frozen=True)
class BranchCandidate:
    s: float                       # arc-length along the centerline
    cluster_count: int             # how many distinct clusters at this s-bin
    replays_in_primary: int        # replays along the main line
    replays_in_alternates: int     # replays in any non-primary cluster
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SegmentBoundary:
    s: float
    reason: str                    # "checkpoint" | "curvature" | "branch_junction"
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteExtractionResult:
    centerline: Centerline
    branches: tuple[BranchCandidate, ...] = ()
    segments: tuple[SegmentBoundary, ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _point_to_json(p: CenterlinePoint) -> dict[str, float]:
    return {"s": p.s, "x": p.x, "y": p.y, "z": p.z}


def _branch_to_json(b: BranchCandidate) -> dict[str, Any]:
    return {
        "s": b.s,
        "cluster_count": b.cluster_count,
        "replays_in_primary": b.replays_in_primary,
        "replays_in_alternates": b.replays_in_alternates,
        "evidence": dict(b.evidence),
    }


def _segment_to_json(seg: SegmentBoundary) -> dict[str, Any]:
    return {"s": seg.s, "reason": seg.reason, "evidence": dict(seg.evidence)}


def to_json(result: RouteExtractionResult) -> dict[str, Any]:
    return {
        "schema_version": ROUTE_ARTIFACT_SCHEMA_VERSION,
        "centerline": [_point_to_json(p) for p in result.centerline.points],
        "branches": [_branch_to_json(b) for b in result.branches],
        "segments": [_segment_to_json(s) for s in result.segments],
    }


def to_canonical_bytes(result: RouteExtractionResult) -> bytes:
    return json.dumps(
        to_json(result), sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def content_hash(result: RouteExtractionResult) -> str:
    return hashlib.sha256(to_canonical_bytes(result)).hexdigest()


def from_json(payload: Mapping[str, Any]) -> RouteExtractionResult:
    version = payload.get("schema_version")
    if version != ROUTE_ARTIFACT_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported route-artifact schema_version {version}; "
            f"this build expects {ROUTE_ARTIFACT_SCHEMA_VERSION}"
        )
    raw_points = payload.get("centerline")
    if not isinstance(raw_points, Sequence) or isinstance(raw_points, (str, bytes)):
        raise ValueError("centerline must be an array")
    points = tuple(
        CenterlinePoint(s=float(p["s"]), x=float(p["x"]), y=float(p["y"]), z=float(p["z"]))
        for p in raw_points
    )
    raw_branches = payload.get("branches", [])
    branches = tuple(
        BranchCandidate(
            s=float(b["s"]),
            cluster_count=int(b["cluster_count"]),
            replays_in_primary=int(b["replays_in_primary"]),
            replays_in_alternates=int(b["replays_in_alternates"]),
            evidence=dict(b.get("evidence", {})),
        )
        for b in raw_branches
    )
    raw_segments = payload.get("segments", [])
    segments = tuple(
        SegmentBoundary(
            s=float(s["s"]),
            reason=str(s["reason"]),
            evidence=dict(s.get("evidence", {})),
        )
        for s in raw_segments
    )
    return RouteExtractionResult(
        centerline=Centerline(points),
        branches=branches,
        segments=segments,
    )
