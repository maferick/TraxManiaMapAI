"""DB orchestrator for route inference.

Flow per map:
1. Collect clean + usable replays whose cohort_membership contains
   the configured target cohort (default: intent).
2. Load telemetry via the pluggable loader (same contract as replay
   cleaning; the wrapper emits a sidecar JSON).
3. Run :class:`RouteExtractor` with the configured clusterer.
4. Serialize the result to ``<artifacts_root>/routes/<hash>.json``.
5. Insert a ``route_artifacts`` row carrying the file path + hash +
   clustering provenance.
6. Emit a summary for the stage_run.

Per-map uniqueness is enforced by ``UNIQUE (map_id, route_version)``
on the route_artifacts table — re-runs with the same ``route_version``
skip existing maps rather than overwriting.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from pymysql.connections import Connection
from pymysql.err import IntegrityError

from src.replay.pipeline import ReplayRow, TelemetryLoader, TelemetryLoadError
from src.replay.telemetry import ReplayTelemetry
from src.route.artifact import (
    RouteExtractionResult,
    content_hash,
    to_canonical_bytes,
    to_json,
)
from src.route.extract import RouteExtractionError, RouteExtractor
from src.schema.replays import ReplayCohort
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass
class RouteStats:
    started_at: datetime
    maps_seen: int = 0
    routes_written: int = 0
    routes_skipped_existing: int = 0
    routes_failed: int = 0
    telemetry_failures: int = 0
    errors: list[str] = field(default_factory=list)
    completed_at: datetime | None = None

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "maps_seen": self.maps_seen,
            "routes_written": self.routes_written,
            "routes_skipped_existing": self.routes_skipped_existing,
            "routes_failed": self.routes_failed,
            "telemetry_failures": self.telemetry_failures,
            "error_count": len(self.errors),
        }


def _write_artifact(
    artifacts_root: Path, result: RouteExtractionResult
) -> tuple[str, Path]:
    digest = content_hash(result)
    dest = artifacts_root / "routes" / digest[:2] / digest[2:4] / f"{digest}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = to_canonical_bytes(result)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(dest)
    return digest, dest


def _insert_route_artifact(
    conn: Connection,
    *,
    map_id: int,
    result: RouteExtractionResult,
    centerline_path: str,
    centerline_hash: str,
    route_version: str,
    clustering_method: str,
    clustering_params: Mapping[str, Any],
    replay_cohort: str,
    created_by_version: str,
    source_artifact_ids: Mapping[str, Any],
) -> bool:
    """Insert the row. Returns False on (map_id, route_version) collision."""
    sql = """
        INSERT INTO route_artifacts (
            map_id, route_version, centerline_path, centerline_hash,
            branches, segment_boundaries,
            clustering_method, clustering_params, replay_cohort,
            extraction_confidence, diagnostics,
            created_by_version, source_artifact_ids
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    payload_json = to_json(result)
    extraction_confidence = result.diagnostics.get("extraction_confidence")
    try:
        with cursor(conn) as cur:
            cur.execute(
                sql,
                (
                    map_id,
                    route_version,
                    centerline_path,
                    centerline_hash,
                    json.dumps(payload_json["branches"]),
                    json.dumps(payload_json["segments"]),
                    clustering_method,
                    json.dumps(dict(clustering_params)),
                    replay_cohort,
                    (
                        round(float(extraction_confidence), 4)
                        if extraction_confidence is not None
                        else None
                    ),
                    json.dumps(result.diagnostics),
                    created_by_version,
                    json.dumps(dict(source_artifact_ids)),
                ),
            )
        conn.commit()
        return True
    except IntegrityError as exc:
        conn.rollback()
        if "uq_route_artifacts" in str(exc).lower() or "1062" in str(exc):
            return False
        raise


def _fetch_candidate_map_ids(
    conn: Connection,
    *,
    snapshot_id: str | None,
    map_ids: Iterable[int] | None,
) -> list[int]:
    sql = (
        "SELECT DISTINCT map_id FROM replays "
        "WHERE clean_status IN ('clean','usable_with_warnings') "
        "AND cohort_membership IS NOT NULL"
    )
    params: list[Any] = []
    if snapshot_id is not None:
        sql += " AND ingestion_snapshot = %s"
        params.append(snapshot_id)
    if map_ids is not None:
        ids = list(map_ids)
        if not ids:
            return []
        placeholders = ",".join(["%s"] * len(ids))
        sql += f" AND map_id IN ({placeholders})"
        params.extend(ids)
    with cursor(conn) as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [int(r[0]) for r in rows]


def _fetch_replays_in_cohort(
    conn: Connection, *, map_id: int, cohort: ReplayCohort
) -> list[ReplayRow]:
    sql = (
        "SELECT id, map_id, raw_artifact_path, raw_artifact_hash, "
        "finish_time_ms, cohort_membership "
        "FROM replays WHERE map_id=%s "
        "AND clean_status IN ('clean','usable_with_warnings') "
        "AND cohort_membership IS NOT NULL"
    )
    with cursor(conn) as cur:
        cur.execute(sql, (map_id,))
        rows = cur.fetchall()
    out: list[ReplayRow] = []
    for r in rows:
        try:
            cohorts = json.loads(r[5]) if r[5] else []
        except json.JSONDecodeError:
            continue
        if cohort.value not in cohorts:
            continue
        out.append(
            ReplayRow(
                id=int(r[0]),
                map_id=int(r[1]),
                raw_artifact_path=r[2],
                raw_artifact_hash=r[3],
                finish_time_ms=(int(r[4]) if r[4] is not None else None),
            )
        )
    return out


class RoutePipeline:
    def __init__(
        self,
        *,
        conn: Connection,
        loader: TelemetryLoader,
        extractor: RouteExtractor,
        artifacts_root: Path,
        route_version: str,
        created_by_version: str,
        clustering_method: str,
        clustering_params: Mapping[str, Any],
        cohort: ReplayCohort = ReplayCohort.INTENT,
        min_replays_per_map: int = 3,
    ) -> None:
        self._conn = conn
        self._loader = loader
        self._extractor = extractor
        self._artifacts_root = artifacts_root
        self._route_version = route_version
        self._created_by_version = created_by_version
        self._clustering_method = clustering_method
        self._clustering_params = dict(clustering_params)
        self._cohort = cohort
        self._min_replays = min_replays_per_map

    def run(
        self,
        *,
        map_ids: Iterable[int] | None = None,
        snapshot_id: str | None = None,
    ) -> RouteStats:
        stats = RouteStats(started_at=_utcnow())
        try:
            candidate_ids = _fetch_candidate_map_ids(
                self._conn, snapshot_id=snapshot_id, map_ids=map_ids
            )
            for mid in candidate_ids:
                stats.maps_seen += 1
                try:
                    self._process_map(mid, stats)
                except Exception as exc:  # noqa: BLE001
                    stats.routes_failed += 1
                    stats.errors.append(f"map={mid}: {exc}")
                    _LOG.exception("route extraction failed for map %d", mid)
        finally:
            stats.completed_at = _utcnow()
        return stats

    def _process_map(self, map_id: int, stats: RouteStats) -> None:
        replays = _fetch_replays_in_cohort(
            self._conn, map_id=map_id, cohort=self._cohort
        )
        if len(replays) < self._min_replays:
            stats.routes_failed += 1
            stats.errors.append(
                f"map={map_id}: only {len(replays)} replays in cohort {self._cohort.value}"
            )
            return
        telemetries: list[ReplayTelemetry] = []
        source_ids: dict[str, Any] = {}
        for r in replays:
            try:
                telemetries.append(self._loader.load(r))
                source_ids[f"replay.{r.id}"] = r.raw_artifact_hash or ""
            except TelemetryLoadError as exc:
                stats.telemetry_failures += 1
                stats.errors.append(f"telemetry replay={r.id}: {exc}")
        if len(telemetries) < self._min_replays:
            stats.routes_failed += 1
            stats.errors.append(
                f"map={map_id}: only {len(telemetries)} telemetries loaded"
            )
            return
        try:
            result = self._extractor.extract(telemetries)
        except RouteExtractionError as exc:
            stats.routes_failed += 1
            stats.errors.append(f"map={map_id}: {exc}")
            return
        digest, dest_path = _write_artifact(self._artifacts_root, result)
        inserted = _insert_route_artifact(
            self._conn,
            map_id=map_id,
            result=result,
            centerline_path=str(dest_path),
            centerline_hash=digest,
            route_version=self._route_version,
            clustering_method=self._clustering_method,
            clustering_params=self._clustering_params,
            replay_cohort=self._cohort.value,
            created_by_version=self._created_by_version,
            source_artifact_ids=source_ids,
        )
        if inserted:
            stats.routes_written += 1
        else:
            stats.routes_skipped_existing += 1
