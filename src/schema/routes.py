"""Route-inference artifact. Schema mirrors
``migrations/mariadb/007_route_artifacts.sql``.

Only the dataclass shape lands in PR 3; actual route-inference logic
arrives in PR 5.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class RouteArtifact:
    id: int | None
    map_id: int
    route_version: str
    centerline_path: str
    centerline_hash: str
    clustering_method: str
    clustering_params: dict[str, Any]
    replay_cohort: str
    created_by_version: str
    source_artifact_ids: dict[str, str]

    branches: list[dict[str, Any]] | None = None
    segment_boundaries: list[dict[str, Any]] | None = None
    extraction_confidence: Decimal | None = None
    diagnostics: dict[str, Any] | None = None
    created_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)
