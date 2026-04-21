"""Dry-run orchestrator.

Walks the union of benchmark-set member maps and an optional community
sample, runs each registered evaluator per map, persists
``evaluation_artifacts`` rows, and returns an in-memory
:class:`DryRunReport` for the markdown renderer.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from pymysql.connections import Connection

from src.benchmarks.manifest import BenchmarkManifest
from src.evaluation.base import Evaluator, EvaluationResult
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass(frozen=True)
class BenchmarkMembership:
    benchmark_version: str
    category: str
    role: str
    label: dict[str, Any]


@dataclass(frozen=True)
class DryRunMap:
    map_id: int
    source_map_id: str
    ingestion_snapshot: str
    memberships: tuple[BenchmarkMembership, ...] = ()

    @property
    def in_any_benchmark(self) -> bool:
        return bool(self.memberships)


@dataclass
class DryRunReport:
    run_id: str
    started_at: datetime
    stage_version: str
    evaluator_ids: tuple[str, ...]
    benchmark_versions: tuple[str, ...]
    maps: list[DryRunMap] = field(default_factory=list)
    results: dict[int, list[EvaluationResult]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    completed_at: datetime | None = None

    def maps_by_membership(self, benchmark_version: str) -> list[DryRunMap]:
        return [
            m
            for m in self.maps
            if any(b.benchmark_version == benchmark_version for b in m.memberships)
        ]

    def community_maps(self) -> list[DryRunMap]:
        return [m for m in self.maps if not m.in_any_benchmark]

    def scores_for(
        self, evaluator_name: str, score_field: str
    ) -> list[tuple[DryRunMap, float]]:
        out: list[tuple[DryRunMap, float]] = []
        for m in self.maps:
            for r in self.results.get(m.map_id, []):
                if r.evaluator_name != evaluator_name:
                    continue
                val = getattr(r, score_field, None)
                if val is None:
                    continue
                out.append((m, float(val)))
                break
        return out

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "maps_total": len(self.maps),
            "maps_in_benchmarks": sum(1 for m in self.maps if m.in_any_benchmark),
            "community_maps": sum(1 for m in self.maps if not m.in_any_benchmark),
            "results_total": sum(len(v) for v in self.results.values()),
            "evaluator_ids": list(self.evaluator_ids),
            "benchmark_versions": list(self.benchmark_versions),
            "error_count": len(self.errors),
        }


def _lookup_benchmark_membership(
    conn: Connection, manifest: BenchmarkManifest
) -> dict[str, int]:
    """Return ``{source_map_id: db_id}`` for every manifest entry present in the DB.

    Entries whose ``map_id`` is not found in the pinned snapshot are
    silently dropped — this is the "benchmark pre-resolution" seam.
    Callers see the dropped count in the diagnostics.
    """
    source_ids = [e.map_id for e in manifest.entries]
    if not source_ids:
        return {}
    placeholders = ",".join(["%s"] * len(source_ids))
    with cursor(conn) as cur:
        cur.execute(
            f"SELECT source_map_id, id FROM maps "
            f"WHERE source_map_id IN ({placeholders}) "
            f"AND ingestion_snapshot = %s",
            (*source_ids, manifest.ingestion_snapshot),
        )
        rows = cur.fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _fetch_community_sample(
    conn: Connection,
    *,
    exclude_ids: set[int],
    sample_size: int,
    snapshot_id: str | None,
) -> list[DryRunMap]:
    if sample_size <= 0:
        return []
    sql = (
        "SELECT id, source_map_id, ingestion_snapshot FROM maps "
        "WHERE parse_status = 'success'"
    )
    params: list[Any] = []
    if snapshot_id is not None:
        sql += " AND ingestion_snapshot = %s"
        params.append(snapshot_id)
    if exclude_ids:
        placeholders = ",".join(["%s"] * len(exclude_ids))
        sql += f" AND id NOT IN ({placeholders})"
        params.extend(sorted(exclude_ids))
    sql += " ORDER BY id LIMIT %s"
    params.append(sample_size)
    with cursor(conn) as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [
        DryRunMap(
            map_id=int(r[0]),
            source_map_id=str(r[1]),
            ingestion_snapshot=str(r[2]),
        )
        for r in rows
    ]


def _persist_result(conn: Connection, result: EvaluationResult) -> None:
    with cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO evaluation_artifacts (
              map_id, evaluator_name, evaluator_version, benchmark_set_version,
              structural_score, drivability_score, flow_score, style_score,
              novelty_score, diversity_metadata, diagnostics, notes,
              code_version, source_artifact_ids
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              structural_score = VALUES(structural_score),
              drivability_score = VALUES(drivability_score),
              flow_score = VALUES(flow_score),
              style_score = VALUES(style_score),
              novelty_score = VALUES(novelty_score),
              diversity_metadata = VALUES(diversity_metadata),
              diagnostics = VALUES(diagnostics),
              notes = VALUES(notes),
              code_version = VALUES(code_version),
              source_artifact_ids = VALUES(source_artifact_ids)
            """,
            (
                result.map_id,
                result.evaluator_name,
                result.evaluator_version,
                result.benchmark_set_version,
                result.structural_score,
                result.drivability_score,
                result.flow_score,
                result.style_score,
                result.novelty_score,
                (
                    json.dumps(result.diversity_metadata)
                    if result.diversity_metadata is not None
                    else None
                ),
                json.dumps(result.diagnostics) if result.diagnostics else None,
                result.notes,
                result.code_version,
                json.dumps(dict(result.source_artifact_ids)),
            ),
        )
    conn.commit()


class DryRunRunner:
    def __init__(
        self,
        *,
        conn: Connection,
        evaluators: Sequence[Evaluator],
        benchmark_manifests: Sequence[BenchmarkManifest] = (),
        community_sample_size: int = 0,
        community_snapshot_id: str | None = None,
        stage_version: str = "0.1.0",
        persist_results: bool = True,
    ) -> None:
        if not evaluators:
            raise ValueError("dry-run requires at least one evaluator")
        self._conn = conn
        self._evaluators = list(evaluators)
        self._manifests = list(benchmark_manifests)
        self._community_sample_size = community_sample_size
        self._community_snapshot_id = community_snapshot_id
        self._stage_version = stage_version
        self._persist_results = persist_results

    def run(self) -> DryRunReport:
        report = DryRunReport(
            run_id=str(uuid.uuid4()),
            started_at=_utcnow(),
            stage_version=self._stage_version,
            evaluator_ids=tuple(f"{e.name}@{e.version}" for e in self._evaluators),
            benchmark_versions=tuple(m.version_id for m in self._manifests),
        )
        try:
            self._resolve_maps(report)
            for m in report.maps:
                per_map_results: list[EvaluationResult] = []
                # Collapse memberships to a comma-joined list or None if community.
                bench_versions = [b.benchmark_version for b in m.memberships]
                benchmark_version = bench_versions[0] if len(bench_versions) == 1 else (
                    ",".join(bench_versions) if bench_versions else None
                )
                for evaluator in self._evaluators:
                    try:
                        r = evaluator.evaluate(
                            m.map_id, benchmark_set_version=benchmark_version
                        )
                    except Exception as exc:  # noqa: BLE001
                        report.errors.append(
                            f"{evaluator.name}@{evaluator.version} map={m.map_id}: {exc}"
                        )
                        _LOG.exception(
                            "evaluator %s failed for map %d", evaluator.name, m.map_id
                        )
                        continue
                    per_map_results.append(r)
                    if self._persist_results:
                        try:
                            _persist_result(self._conn, r)
                        except Exception as exc:  # noqa: BLE001
                            report.errors.append(
                                f"persist {evaluator.name} map={m.map_id}: {exc}"
                            )
                            _LOG.exception("persist failed for map %d", m.map_id)
                report.results[m.map_id] = per_map_results
        finally:
            report.completed_at = _utcnow()
        return report

    def _resolve_maps(self, report: DryRunReport) -> None:
        memberships_by_db_id: dict[int, list[BenchmarkMembership]] = {}
        db_id_to_source: dict[int, tuple[str, str]] = {}

        for manifest in self._manifests:
            db_ids = _lookup_benchmark_membership(self._conn, manifest)
            for source_id, db_id in db_ids.items():
                db_id_to_source[db_id] = (source_id, manifest.ingestion_snapshot)
                entry = next(
                    (e for e in manifest.entries if e.map_id == source_id), None
                )
                if entry is None:
                    continue
                memberships_by_db_id.setdefault(db_id, []).append(
                    BenchmarkMembership(
                        benchmark_version=manifest.version_id,
                        category=manifest.category,
                        role=entry.role,
                        label=dict(entry.label),
                    )
                )
            missing = len(manifest.entries) - len(db_ids)
            if missing:
                _LOG.warning(
                    "benchmark %s: %d/%d entries missing from snapshot %s",
                    manifest.version_id,
                    missing,
                    len(manifest.entries),
                    manifest.ingestion_snapshot,
                )

        for db_id, memberships in memberships_by_db_id.items():
            source_id, snapshot = db_id_to_source[db_id]
            report.maps.append(
                DryRunMap(
                    map_id=db_id,
                    source_map_id=source_id,
                    ingestion_snapshot=snapshot,
                    memberships=tuple(memberships),
                )
            )

        community = _fetch_community_sample(
            self._conn,
            exclude_ids=set(memberships_by_db_id.keys()),
            sample_size=self._community_sample_size,
            snapshot_id=self._community_snapshot_id,
        )
        report.maps.extend(community)
