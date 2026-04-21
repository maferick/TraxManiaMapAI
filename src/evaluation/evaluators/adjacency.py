"""Adjacency-graph evaluator.

Queries the constraint graph (PR 6) for every adjacency this map
contributes and reports the validity-label breakdown. The score is
``1 - suspicious_fraction`` — an edge labeled ``suspicious`` (by the
broken-fixture policy in ``src/constraints/evidence.py``) pulls the
score down, while ``valid`` and ``unknown`` edges do not. This keeps
alignment with the "no frequency-as-validity" invariant: ``unknown``
is not penalized, only explicit evidence-of-badness is.
"""
from __future__ import annotations

import neo4j
from pymysql.connections import Connection

from src.constraints.extractor import extract_adjacencies
from src.evaluation.base import Evaluator, EvaluationResult, utcnow
from src.evaluation.evaluators.structural import _fetch_placements
from src.evaluation.registry import register
from src.utils.config import code_version


_QUERY = """
UNWIND $pairs AS p
MATCH (:Block {key: p.a})-[r:ADJACENT_TO]->(:Block {key: p.b})
RETURN p.a AS a, p.b AS b,
       coalesce(r.validity_label, 'unknown') AS label
"""


@register
class AdjacencyGraphEvaluator(Evaluator):
    name = "adjacency_graph"
    version = "0.1.0"

    def __init__(
        self,
        conn: Connection,
        driver: neo4j.Driver,
        *,
        parser_version: str = "0.0.0",
    ) -> None:
        self._conn = conn
        self._driver = driver
        self._parser_version = parser_version

    def evaluate(
        self,
        map_id: int,
        *,
        benchmark_set_version: str | None = None,
    ) -> EvaluationResult:
        placements = _fetch_placements(
            self._conn, map_id=map_id, parser_version=self._parser_version
        )
        diagnostics: dict[str, object] = {
            "parser_version": self._parser_version,
            "placements_count": len(placements),
        }

        structural_score: float | None
        if not placements:
            structural_score = None
            diagnostics["reason"] = "no_placements"
            return self._result(map_id, benchmark_set_version, structural_score, diagnostics)

        observations = extract_adjacencies(
            placements, snapshot_id="__eval__", is_benchmark_strong=False, is_broken_fixture=False
        )
        diagnostics["adjacencies_in_map"] = len(observations)

        if not observations:
            structural_score = None
            diagnostics["reason"] = "no_adjacencies"
            return self._result(map_id, benchmark_set_version, structural_score, diagnostics)

        pairs = [
            {"a": o.a.normalized_key, "b": o.b.normalized_key} for o in observations
        ]
        with self._driver.session() as session:
            records = session.run(_QUERY, pairs=pairs).data()
        labels = {"valid": 0, "suspicious": 0, "unknown": 0}
        matched = 0
        for rec in records:
            label = rec.get("label") or "unknown"
            labels[label] = labels.get(label, 0) + 1
            matched += 1
        # Any pair not matched in the graph is treated as "unknown".
        unmatched = len(observations) - matched
        labels["unknown"] = labels.get("unknown", 0) + unmatched

        total = len(observations)
        suspicious_fraction = labels["suspicious"] / total
        structural_score = 1.0 - suspicious_fraction
        diagnostics["label_counts"] = labels
        diagnostics["unmatched_pairs"] = unmatched
        diagnostics["suspicious_fraction"] = round(suspicious_fraction, 6)

        return self._result(map_id, benchmark_set_version, structural_score, diagnostics)

    def _result(
        self,
        map_id: int,
        benchmark_set_version: str | None,
        structural_score: float | None,
        diagnostics: dict[str, object],
    ) -> EvaluationResult:
        return EvaluationResult(
            map_id=map_id,
            evaluator_name=self.name,
            evaluator_version=self.version,
            benchmark_set_version=benchmark_set_version,
            created_at=utcnow(),
            code_version=code_version(),
            source_artifact_ids={"map": str(map_id), "parser": self._parser_version},
            structural_score=structural_score,
            diagnostics=diagnostics,
        )
