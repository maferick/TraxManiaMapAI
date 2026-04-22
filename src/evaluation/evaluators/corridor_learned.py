"""route_corridor_learned@0.1.0 — per-map drivability from the
learned corridor score.

Parallel to :class:`CorridorConfidenceEvaluator` — same per-map
aggregation (mean over top-rank corridors, ``path_rank = 0``), but
reads ``route_corridors.learned_corridor_score`` instead of
``corridor_confidence``. Keeping the two evaluators parallel lets
the dry-run compare them on the same map set without any bespoke
per-cohort logic.

Honest framing (load-bearing — don't let the number look stronger
than it is):

- The learned score is trained on WEAK OBSERVED LABELS
  (``time_envelope`` plausibility on replay-rich maps; falls back to
  ``inverse_rank`` synthetic on maps without replays). It is not a
  ground-truth drivability score.
- Maps without a persisted learned_corridor_score emit ``None`` —
  same pattern as the heuristic evaluator when nothing has been
  scored yet.
- Interpret the comparison against ``route_corridor`` as a direction,
  not a winner. Ranking-stability and disagreement analysis live
  upstream in the dry-run report.

Why the score is not clipped here:

- ``learned_corridor_score`` is the raw ridge prediction and may
  fall outside [0, 1]. For the per-map MEAN we preserve the sign +
  magnitude so the dry-run can flag score-distribution drift. If a
  future consumer needs a bounded "comparable" score, it should
  clip at read time, not persist a second column.
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
class CorridorLearnedEvaluator(Evaluator):
    name = "route_corridor_learned"
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
                """
                SELECT src_tag, src_order, dst_tag, dst_order,
                       learned_corridor_score, path_length,
                       contains_virtual_edge, learned_score_version,
                       learned_score_model_hash
                FROM route_corridors
                WHERE map_id = %s
                  AND path_rank = 0
                  AND learned_corridor_score IS NOT NULL
                """,
                (map_id,),
            )
            rows = cur.fetchall()

        diagnostics: dict[str, Any] = {"interval_count": len(rows)}
        drivability: float | None
        if not rows:
            diagnostics["reason"] = "no_learned_scores"
            drivability = None
        else:
            scores = [float(r[4]) for r in rows]
            lengths = [int(r[5]) for r in rows]
            virtuals = sum(1 for r in rows if int(r[6]))
            drivability = statistics.mean(scores)
            diagnostics["learned_score_mean"] = round(drivability, 4)
            diagnostics["learned_score_min"] = round(min(scores), 4)
            diagnostics["learned_score_max"] = round(max(scores), 4)
            if len(scores) >= 2:
                diagnostics["learned_score_stdev"] = round(
                    statistics.stdev(scores), 4
                )
            diagnostics["path_length_median"] = statistics.median(lengths)
            diagnostics["virtual_edge_fraction"] = round(virtuals / len(rows), 4)
            # Provenance — which model produced these rows. All rows on
            # one map share the same version + hash after a single
            # score-corridors-learned pass; we sample the first row.
            diagnostics["learned_score_version"] = str(rows[0][7])
            diagnostics["model_hash"] = str(rows[0][8])[:12]

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
