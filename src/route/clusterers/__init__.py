"""Route-inference clusterers. All pluggable via :func:`create`."""
from src.route.clusterers.base import (
    ClusterResult,
    Clusterer,
    ClustererUnavailableError,
    all_registered,
    create,
    get,
    register,
)
# Import concrete impls for their side-effect of registering.
from src.route.clusterers.dbscan import DbscanClusterer  # noqa: F401
from src.route.clusterers.grid import GridClusterer  # noqa: F401
from src.route.clusterers.per_segment import PerSegmentClusterer  # noqa: F401

__all__ = [
    "ClusterResult",
    "Clusterer",
    "ClustererUnavailableError",
    "DbscanClusterer",
    "GridClusterer",
    "PerSegmentClusterer",
    "all_registered",
    "create",
    "get",
    "register",
]
