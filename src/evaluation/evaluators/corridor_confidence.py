"""route_corridor@0.1.0 — per-map drivability from corridor confidence.

Aggregates ``route_corridors.corridor_confidence`` (populated by
``src.corridor.scoring.score_corridor``) across a map's top-rank
corridors into a single map-level score. Per interval, only the
top-ranked corridor (``path_rank = 0``, shortest + lex tiebreak)
contributes — the evaluator measures "how good is the BEST corridor
for each interval," not "how consistent are all corridors."

The emitted ``drivability_score`` is the arithmetic mean of top-rank
confidences across the map's intervals. Maps with no corridors
(never built, or no grid intervals found during enumeration) emit
``None`` — mirrors the no-invented-scores pattern the other
evaluators use. Diagnostics carry the interval count, min/max
confidences, and virtual-edge fraction so the dry-run report can
surface those alongside the headline number.

Non-goals:

- no attempt to distinguish "good map quality" from "good corridor
  inference" — corridor_confidence is bounded by our classification +
  signal wiring, not by ground truth. A low score on a quality map
  can mean "our signals didn't find it" rather than "the map is bad."
  The evaluator surfaces the number; it does not claim it's a
  quality judgment.
- no weighting by path_rank — all rank-0 corridors count equally.
  Future versions may weight by interval criticality or by
  per-corridor path_length.
"""
from __future__ import annotations

import statistics
from typing import Any

from pymysql.connections import Connection

from src.evaluation.base import Evaluator, EvaluationResult, utcnow
from src.evaluation.registry import register
from src.storage.mariadb import cursor
from src.utils.config import code_version


@register
class CorridorConfidenceEvaluator(Evaluator):
    name = "route_corridor"
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
            # One row per interval — the top-rank corridor, by the
            # (length, lex) ordering materialized at build time.
            cur.execute(
                """
                SELECT src_tag, src_order, dst_tag, dst_order,
                       corridor_confidence, path_length, contains_virtual_edge
                FROM route_corridors
                WHERE map_id = %s
                  AND path_rank = 0
                  AND corridor_confidence IS NOT NULL
                """,
                (map_id,),
            )
            rows = cur.fetchall()

        diagnostics: dict[str, Any] = {"interval_count": len(rows)}
        drivability: float | None
        if not rows:
            diagnostics["reason"] = "no_scored_corridors"
            drivability = None
        else:
            confidences = [float(r[4]) for r in rows]
            lengths = [int(r[5]) for r in rows]
            virtuals = sum(1 for r in rows if int(r[6]))
            drivability = statistics.mean(confidences)
            diagnostics["corridor_confidence_mean"] = round(drivability, 4)
            diagnostics["corridor_confidence_min"] = round(min(confidences), 4)
            diagnostics["corridor_confidence_max"] = round(max(confidences), 4)
            if len(confidences) >= 2:
                diagnostics["corridor_confidence_stdev"] = round(
                    statistics.stdev(confidences), 4
                )
            diagnostics["path_length_median"] = statistics.median(lengths)
            diagnostics["virtual_edge_fraction"] = round(virtuals / len(rows), 4)

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
