"""Graph-build pipeline.

Walks maps in MariaDB, extracts adjacencies, writes to Neo4j with
per-map idempotency (tracked via ``:ProcessedMap`` nodes) and
evidence accumulation that respects the "no frequency-as-validity"
rule. Validity labels are recomputed from current evidence counts on
every edge write.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import neo4j
from pymysql.connections import Connection

from src.constraints.extractor import extract_adjacencies, unique_block_keys
from src.constraints.nodes import AdjacencyObservation, BlockKey
from src.schema.maps import BlockPlacement
from src.storage.mariadb import cursor as mariadb_cursor

_LOG = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass(frozen=True)
class _MapRow:
    id: int
    parser_version: str


@dataclass
class BuildStats:
    started_at: datetime
    maps_seen: int = 0
    maps_processed: int = 0
    maps_skipped_already_processed: int = 0
    maps_skipped_no_placements: int = 0
    observations_emitted: int = 0
    nodes_merged: int = 0
    edges_merged: int = 0
    errors: list[str] = field(default_factory=list)
    completed_at: datetime | None = None

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "maps_seen": self.maps_seen,
            "maps_processed": self.maps_processed,
            "maps_skipped_already_processed": self.maps_skipped_already_processed,
            "maps_skipped_no_placements": self.maps_skipped_no_placements,
            "observations_emitted": self.observations_emitted,
            "nodes_merged": self.nodes_merged,
            "edges_merged": self.edges_merged,
            "error_count": len(self.errors),
        }


def _fetch_candidate_maps(
    conn: Connection,
    *,
    snapshot_id: str | None,
    map_ids: Iterable[int] | None,
    parser_version: str | None,
) -> list[_MapRow]:
    sql = "SELECT id, parser_version FROM maps WHERE parse_status = 'success'"
    params: list[Any] = []
    if snapshot_id is not None:
        sql += " AND ingestion_snapshot = %s"
        params.append(snapshot_id)
    if parser_version is not None:
        sql += " AND parser_version = %s"
        params.append(parser_version)
    if map_ids is not None:
        ids = list(map_ids)
        if not ids:
            return []
        placeholders = ",".join(["%s"] * len(ids))
        sql += f" AND id IN ({placeholders})"
        params.extend(ids)
    sql += " ORDER BY id"
    with mariadb_cursor(conn) as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [_MapRow(id=int(r[0]), parser_version=str(r[1])) for r in rows]


def _fetch_placements(
    conn: Connection, *, map_id: int, parser_version: str
) -> list[BlockPlacement]:
    with mariadb_cursor(conn) as cur:
        cur.execute(
            """
            SELECT id, parser_version, block_family, block_type, variant,
                   placement_index, x, y, z, rotation, flags, surface,
                   created_by_version, source_artifact_ids
            FROM block_placements
            WHERE map_id = %s AND parser_version = %s
            ORDER BY placement_index
            """,
            (map_id, parser_version),
        )
        rows = cur.fetchall()
    out: list[BlockPlacement] = []
    for r in rows:
        out.append(
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
        )
    return out


_CHECK_OR_CLAIM_QUERY = """
MERGE (p:ProcessedMap {
    map_id: $map_id,
    snapshot_id: $snapshot_id,
    parser_version: $parser_version
})
ON CREATE SET p.processed_at = datetime(), p.stage_version = $stage_version, p.created = true
ON MATCH SET p.created = false
RETURN p.created AS was_new
"""


_MERGE_NODES_QUERY = """
UNWIND $nodes AS n
MERGE (b:Block {key: n.key})
  ON CREATE SET b.family = n.family, b.type = n.type, b.variant = n.variant
RETURN count(b) AS merged
"""


_MERGE_EDGES_QUERY = """
UNWIND $edges AS e
MATCH (a:Block {key: e.a_key}), (b:Block {key: e.b_key})
MERGE (a)-[r:ADJACENT_TO]->(b)
  ON CREATE SET
    r.observed_in_maps_count = 1,
    r.benchmark_strong_count = e.bench_delta,
    r.broken_fixture_count = e.broken_delta,
    r.replay_supported_count = 0,
    r.first_seen_snapshot = e.snapshot,
    r.last_seen_snapshot = e.snapshot,
    r.last_updated_at = datetime()
  ON MATCH SET
    r.observed_in_maps_count = coalesce(r.observed_in_maps_count, 0) + 1,
    r.benchmark_strong_count = coalesce(r.benchmark_strong_count, 0) + e.bench_delta,
    r.broken_fixture_count = coalesce(r.broken_fixture_count, 0) + e.broken_delta,
    r.last_seen_snapshot = e.snapshot,
    r.last_updated_at = datetime()
WITH r
SET r.validity_label = CASE
    WHEN r.benchmark_strong_count >= 1 THEN 'valid'
    WHEN r.broken_fixture_count > 0
         AND r.benchmark_strong_count = 0
         AND coalesce(r.replay_supported_count, 0) = 0 THEN 'suspicious'
    ELSE 'unknown'
END
RETURN count(r) AS merged
"""


def _claim_map(
    tx: neo4j.ManagedTransaction,
    *,
    map_id: int,
    snapshot_id: str,
    parser_version: str,
    stage_version: str,
) -> bool:
    result = tx.run(
        _CHECK_OR_CLAIM_QUERY,
        map_id=map_id,
        snapshot_id=snapshot_id,
        parser_version=parser_version,
        stage_version=stage_version,
    )
    record = result.single()
    return bool(record["was_new"]) if record else False


def _merge_graph(
    tx: neo4j.ManagedTransaction,
    *,
    keys: Sequence[BlockKey],
    observations: Sequence[AdjacencyObservation],
) -> tuple[int, int]:
    nodes_payload = [
        {
            "key": k.normalized_key,
            "family": k.family,
            "type": k.type,
            "variant": k.variant,
        }
        for k in keys
    ]
    edges_payload = [
        {
            "a_key": o.a.normalized_key,
            "b_key": o.b.normalized_key,
            "snapshot": o.snapshot_id,
            "bench_delta": 1 if o.is_benchmark_strong else 0,
            "broken_delta": 1 if o.is_broken_fixture else 0,
        }
        for o in observations
    ]
    n_result = tx.run(_MERGE_NODES_QUERY, nodes=nodes_payload).single()
    e_result = tx.run(_MERGE_EDGES_QUERY, edges=edges_payload).single() if edges_payload else None
    return (
        int(n_result["merged"]) if n_result else 0,
        int(e_result["merged"]) if e_result else 0,
    )


class ConstraintGraphPipeline:
    def __init__(
        self,
        *,
        mariadb: Connection,
        neo4j_driver: neo4j.Driver,
        stage_version: str,
        benchmark_strong_map_ids: set[int] | None = None,
        broken_fixture_map_ids: set[int] | None = None,
    ) -> None:
        self._mariadb = mariadb
        self._neo4j = neo4j_driver
        self._stage_version = stage_version
        self._bench = set(benchmark_strong_map_ids or set())
        self._broken = set(broken_fixture_map_ids or set())

    def run(
        self,
        *,
        snapshot_id: str | None = None,
        map_ids: Iterable[int] | None = None,
        parser_version: str | None = None,
    ) -> BuildStats:
        stats = BuildStats(started_at=_utcnow())
        try:
            candidates = _fetch_candidate_maps(
                self._mariadb,
                snapshot_id=snapshot_id,
                map_ids=map_ids,
                parser_version=parser_version,
            )
            for row in candidates:
                stats.maps_seen += 1
                try:
                    self._process_one(row, snapshot_id=snapshot_id, stats=stats)
                except Exception as exc:  # noqa: BLE001
                    stats.errors.append(f"map={row.id}: {exc}")
                    _LOG.exception("graph build failed for map %d", row.id)
        finally:
            stats.completed_at = _utcnow()
        return stats

    def _process_one(
        self,
        row: _MapRow,
        *,
        snapshot_id: str | None,
        stats: BuildStats,
    ) -> None:
        placements = _fetch_placements(
            self._mariadb, map_id=row.id, parser_version=row.parser_version
        )
        if not placements:
            stats.maps_skipped_no_placements += 1
            return

        # Resolve the map's snapshot id if the caller didn't constrain it.
        effective_snapshot = snapshot_id
        if effective_snapshot is None:
            with mariadb_cursor(self._mariadb) as cur:
                cur.execute("SELECT ingestion_snapshot FROM maps WHERE id=%s", (row.id,))
                r = cur.fetchone()
            if r is None:
                stats.errors.append(f"map={row.id}: disappeared mid-run")
                return
            effective_snapshot = str(r[0])

        with self._neo4j.session() as session:
            was_new = session.execute_write(
                _claim_map,
                map_id=row.id,
                snapshot_id=effective_snapshot,
                parser_version=row.parser_version,
                stage_version=self._stage_version,
            )
            if not was_new:
                stats.maps_skipped_already_processed += 1
                return

            observations = extract_adjacencies(
                placements,
                snapshot_id=effective_snapshot,
                is_benchmark_strong=row.id in self._bench,
                is_broken_fixture=row.id in self._broken,
            )
            if not observations:
                stats.maps_processed += 1
                return

            keys = unique_block_keys(observations)
            nodes_merged, edges_merged = session.execute_write(
                _merge_graph, keys=keys, observations=observations
            )

        stats.maps_processed += 1
        stats.observations_emitted += len(observations)
        stats.nodes_merged += nodes_merged
        stats.edges_merged += edges_merged
