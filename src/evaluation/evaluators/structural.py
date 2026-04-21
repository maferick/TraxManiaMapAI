"""Pure-block structural evaluator.

Proxy for structural validity: a block with zero axis-neighbors
(Manhattan distance 1) is an "orphan" — a strong indicator the map
is broken or disconnected. The scaffold score is
``1 - orphan_fraction``; empty placements produce ``None``.

This is not the final structural validator. It doesn't check:
- reachability from start to finish
- connectivity via the real block-connection graph (needs PR 6+
  directed-transition data)
- loop / cycle topology

Those land alongside a proper validator once GBX-metadata is
present. The PR 7 dry-run needs *some* structural signal to
demonstrate the evaluator stack produces a report — orphan-fraction
is that signal.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from pymysql.connections import Connection

from src.evaluation.base import Evaluator, EvaluationResult, utcnow
from src.evaluation.registry import register
from src.schema.maps import BlockPlacement
from src.storage.mariadb import cursor
from src.utils.config import code_version


_NEIGHBOR_OFFSETS = (
    (1, 0, 0),
    (-1, 0, 0),
    (0, 1, 0),
    (0, -1, 0),
    (0, 0, 1),
    (0, 0, -1),
)


def _count_orphans(cells: set[tuple[int, int, int]]) -> int:
    orphans = 0
    for (x, y, z) in cells:
        has_neighbor = any(
            (x + dx, y + dy, z + dz) in cells for dx, dy, dz in _NEIGHBOR_OFFSETS
        )
        if not has_neighbor:
            orphans += 1
    return orphans


def _fetch_placements(
    conn: Connection, *, map_id: int, parser_version: str
) -> list[BlockPlacement]:
    with cursor(conn) as cur:
        cur.execute(
            "SELECT id, parser_version, block_family, block_type, variant, "
            "placement_index, x, y, z, rotation, flags, surface, "
            "created_by_version, source_artifact_ids "
            "FROM block_placements WHERE map_id=%s AND parser_version=%s "
            "ORDER BY placement_index",
            (map_id, parser_version),
        )
        rows = cur.fetchall()
    return [
        BlockPlacement(
            id=int(r[0]),
            map_id=map_id,
            parser_version=str(r[1]),
            block_family=str(r[2]),
            block_type=str(r[3]),
            variant=(str(r[4]) if r[4] is not None else None),
            placement_index=int(r[5]),
            x=int(r[6]),
            y=int(r[7]),
            z=int(r[8]),
            rotation=int(r[9]),
            flags=(int(r[10]) if r[10] is not None else None),
            surface=(str(r[11]) if r[11] is not None else None),
            created_by_version=str(r[12]),
            source_artifact_ids={},
        )
        for r in rows
    ]


@register
class StructuralEvaluator(Evaluator):
    name = "structural"
    version = "0.1.0"

    def __init__(self, conn: Connection, *, parser_version: str = "0.0.0") -> None:
        self._conn = conn
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
        total = len(placements)
        diagnostics: dict[str, Any] = {
            "total_blocks": total,
            "parser_version": self._parser_version,
        }
        structural_score: float | None
        if total == 0:
            structural_score = None
            diagnostics["reason"] = "no_placements"
        else:
            cells = {(p.x, p.y, p.z) for p in placements}
            orphans = _count_orphans(cells)
            structural_score = 1.0 - (orphans / total)
            diagnostics["orphan_count"] = orphans
            diagnostics["unique_cells"] = len(cells)
            family_counts = Counter(p.block_family for p in placements)
            diagnostics["block_family_counts"] = dict(family_counts)

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
