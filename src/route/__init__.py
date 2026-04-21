"""Route inference. PR 5 scaffold.

See docs/architecture.md for the subsystem overview and
src/route/README.md for the on-ramp.
"""
from src.route.artifact import (
    ROUTE_ARTIFACT_SCHEMA_VERSION,
    BranchCandidate,
    Centerline,
    CenterlinePoint,
    RouteExtractionResult,
    SegmentBoundary,
    content_hash,
    from_json,
    to_canonical_bytes,
    to_json,
)
from src.route.clusterers import (
    ClusterResult,
    Clusterer,
    ClustererUnavailableError,
    DbscanClusterer,
    GridClusterer,
    PerSegmentClusterer,
    all_registered,
    create,
    get,
    register,
)
from src.route.extract import RouteExtractionError, RouteExtractor
from src.route.pipeline import RoutePipeline, RouteStats
from src.route.projection import (
    ProjectedSamples,
    build_initial_centerline,
    project_points,
    project_telemetry,
    telemetry_to_xyz,
)

__all__ = [
    "ROUTE_ARTIFACT_SCHEMA_VERSION",
    "BranchCandidate",
    "Centerline",
    "CenterlinePoint",
    "ClusterResult",
    "Clusterer",
    "ClustererUnavailableError",
    "DbscanClusterer",
    "GridClusterer",
    "PerSegmentClusterer",
    "ProjectedSamples",
    "RouteExtractionError",
    "RouteExtractionResult",
    "RouteExtractor",
    "RoutePipeline",
    "RouteStats",
    "SegmentBoundary",
    "all_registered",
    "build_initial_centerline",
    "content_hash",
    "create",
    "from_json",
    "get",
    "project_points",
    "project_telemetry",
    "register",
    "telemetry_to_xyz",
    "to_canonical_bytes",
    "to_json",
]
