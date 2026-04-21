"""Route-coverage evaluator.

Surfaces the most recent :class:`RouteArtifact` for a map and reads
``extraction_confidence`` into ``drivability_score``. Intentionally
lightweight: it only wraps the route-inference artifact; it does not
re-compute anything. Maps without a route artifact emit ``None``
rather than an invented score.
"""
from __future__ import annotations

import json
from typing import Any

from pymysql.connections import Connection

from src.evaluation.base import Evaluator, EvaluationResult, utcnow
from src.evaluation.registry import register
from src.storage.mariadb import cursor
from src.utils.config import code_version


@register
class RouteCoverageEvaluator(Evaluator):
    name = "route_coverage"
    version = "0.1.0"

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def evaluate(
        self,
        map_id: int,
        *,
        benchmark_set_version: str | None = None,
    ) -> EvaluationResult:
        with cursor(self._conn) as cur:
            cur.execute(
                "SELECT route_version, extraction_confidence, diagnostics, "
                "clustering_method, centerline_path "
                "FROM route_artifacts WHERE map_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (map_id,),
            )
            row = cur.fetchone()

        diagnostics: dict[str, Any] = {}
        drivability: float | None = None
        if row is None:
            diagnostics["reason"] = "no_route_artifact"
        else:
            route_version, confidence, route_diag_json, clustering_method, path = row
            diagnostics["route_version"] = str(route_version)
            diagnostics["clustering_method"] = str(clustering_method)
            diagnostics["centerline_path"] = str(path)
            if confidence is not None:
                drivability = float(confidence)
                diagnostics["extraction_confidence"] = drivability
            if route_diag_json:
                try:
                    route_diag = json.loads(route_diag_json)
                except json.JSONDecodeError:
                    route_diag = None
                if isinstance(route_diag, dict):
                    for key in ("n_replays", "mean_lateral_distance_m", "n_clusters"):
                        if key in route_diag:
                            diagnostics[f"route_{key}"] = route_diag[key]

        return EvaluationResult(
            map_id=map_id,
            evaluator_name=self.name,
            evaluator_version=self.version,
            benchmark_set_version=benchmark_set_version,
            created_at=utcnow(),
            code_version=code_version(),
            source_artifact_ids={"map": str(map_id)},
            drivability_score=drivability,
            diagnostics=diagnostics,
        )
