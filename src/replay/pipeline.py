"""Replay-clean + cohort-assignment orchestrators.

Two stages are exposed:

- :class:`ReplayCleanPipeline` — per-replay; runs the rules, writes
  status + diagnostics.
- :class:`CohortAssignmentPipeline` — per-map; computes cohort
  membership over the finished-clean replay distribution.

Cohort assignment depends on per-map distribution, so it runs after
cleaning has produced enough classified replays on a map to be
meaningful. The two stages emit separate ``stage_run`` rows.
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

from src.replay.breadcrumbs import (
    BreadcrumbsLoadError,
    FileBreadcrumbLoader,
    ReplayBreadcrumbs,
)
from src.replay.classify import ClassificationOutcome, classify
from src.replay.cohorts import (
    CohortAssignmentConfig,
    MapCohortStats,
    assign_cohorts_for_map,
    summarize,
)
from src.replay.rules.base import Rule, run_rules
from src.replay.rules.breadcrumb import (
    BreadcrumbRule,
    default_breadcrumb_rules,
    run_breadcrumb_rules,
)
from src.replay.telemetry import ReplayTelemetry, from_dict
from src.schema.replays import CleanStatus, ReplayCohort
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class TelemetryLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReplayRow:
    id: int
    map_id: int
    raw_artifact_path: str | None
    raw_artifact_hash: str | None
    finish_time_ms: int | None


class TelemetryLoader(Protocol):
    def load(self, replay: ReplayRow) -> ReplayTelemetry: ...


class FileTelemetryLoader:
    """Reads ``<raw_artifact_path>.telemetry.json`` emitted by the wrapper.

    Sidecars with ``samples: []`` are treated as a clean load failure
    (``TelemetryLoadError``) rather than a format error. The current
    TM2020 GBX wrapper can't decode position telemetry (entity-record
    stream requires format-specific decoding GBX.NET doesn't expose),
    so empty-samples sidecars are the expected signal that telemetry
    isn't extractable. The pipeline routes these to the clean
    ``telemetry_unavailable`` rejection path with no samples of
    position-dependent rules raising.
    """

    def load(self, replay: ReplayRow) -> ReplayTelemetry:
        if not replay.raw_artifact_path:
            raise TelemetryLoadError(f"replay id={replay.id} has no raw_artifact_path")
        path = Path(replay.raw_artifact_path + ".telemetry.json")
        if not path.is_file():
            raise TelemetryLoadError(f"telemetry sidecar missing: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TelemetryLoadError(f"{path} is not valid JSON: {exc}") from exc
        samples = payload.get("samples")
        if not samples:
            raise TelemetryLoadError(
                f"sidecar {path.name} has no position samples "
                "(TM2020 entity-record decoding not supported by this wrapper build)"
            )
        return from_dict(payload)


@dataclass
class CleanStats:
    started_at: datetime
    replays_seen: int = 0
    replays_clean: int = 0
    replays_usable_with_warnings: int = 0
    replays_rejected: int = 0
    load_failures: int = 0
    rule_exceptions: int = 0
    # Track the breadcrumb-fallback path separately so we can see what
    # fraction of the cohort is coming from partial signal. These count
    # are subsets of replays_clean / replays_usable_with_warnings.
    breadcrumb_path_used: int = 0
    errors: list[str] = field(default_factory=list)
    completed_at: datetime | None = None

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "replays_seen": self.replays_seen,
            "replays_clean": self.replays_clean,
            "replays_usable_with_warnings": self.replays_usable_with_warnings,
            "replays_rejected": self.replays_rejected,
            "load_failures": self.load_failures,
            "rule_exceptions": self.rule_exceptions,
            "breadcrumb_path_used": self.breadcrumb_path_used,
            "error_count": len(self.errors),
        }


def _fetch_unprocessed(
    conn: Connection,
    *,
    snapshot_id: str | None,
    limit: int | None,
) -> list[ReplayRow]:
    sql = (
        "SELECT id, map_id, raw_artifact_path, raw_artifact_hash, finish_time_ms "
        "FROM replays WHERE clean_status = 'unprocessed'"
    )
    params: list[Any] = []
    if snapshot_id is not None:
        sql += " AND ingestion_snapshot = %s"
        params.append(snapshot_id)
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(int(limit))
    with cursor(conn) as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [
        ReplayRow(
            id=int(r[0]),
            map_id=int(r[1]),
            raw_artifact_path=r[2],
            raw_artifact_hash=r[3],
            finish_time_ms=(int(r[4]) if r[4] is not None else None),
        )
        for r in rows
    ]


def _write_classification(
    conn: Connection,
    *,
    replay_id: int,
    outcome: ClassificationOutcome,
    clean_version: str,
    signal_source: str = "telemetry",
    extra_diagnostics: Mapping[str, Any] | None = None,
) -> None:
    """Persist the classification. ``signal_source`` lands in diagnostics
    so downstream can tell telemetry-based from breadcrumb-only cleanings
    apart without re-reading the sidecar.
    """
    payload = outcome.diagnostics_payload()
    payload["signal_source"] = signal_source
    if extra_diagnostics:
        payload.update(extra_diagnostics)
    with cursor(conn) as cur:
        cur.execute(
            "UPDATE replays SET clean_status=%s, clean_version=%s, clean_diagnostics=%s "
            "WHERE id=%s",
            (
                outcome.status.value,
                clean_version,
                json.dumps(payload),
                replay_id,
            ),
        )
    conn.commit()


def _write_telemetry_unavailable(
    conn: Connection,
    *,
    replay_id: int,
    clean_version: str,
    detail: str,
) -> None:
    diagnostics = {
        "status": CleanStatus.REJECTED.value,
        "triggered": ["telemetry_unavailable"],
        "rejection_reasons": [f"telemetry_unavailable:{detail[:200]}"],
        "rules": [],
    }
    with cursor(conn) as cur:
        cur.execute(
            "UPDATE replays SET clean_status=%s, clean_version=%s, clean_diagnostics=%s "
            "WHERE id=%s",
            (CleanStatus.REJECTED.value, clean_version, json.dumps(diagnostics), replay_id),
        )
    conn.commit()


class ReplayCleanPipeline:
    """Cleans replays using telemetry when samples are present, and
    falls back to a breadcrumb-only rule set when they're not (the
    expected path for offline-parsed TM2020 replays — see
    ``docs/reverse-engineering/tm2020-replay-telemetry-spike.md``).

    When both ``breadcrumb_loader`` and ``breadcrumb_rules`` are
    provided, a :class:`TelemetryLoadError` on the primary loader
    triggers a second attempt using breadcrumbs. If the breadcrumb
    sidecar is also unavailable or unparseable, the replay is
    rejected as ``telemetry_unavailable`` exactly as before.

    The breadcrumb path's signal source is tracked in the diagnostics
    payload (``signal_source: "breadcrumbs"``) and counted separately
    in :class:`CleanStats` so the operator can see what fraction of a
    cohort was classified from partial signal.
    """

    def __init__(
        self,
        *,
        conn: Connection,
        loader: TelemetryLoader,
        rules: Sequence[Rule],
        thresholds_by_rule: Mapping[str, Mapping[str, Any]] | None,
        clean_version: str,
        breadcrumb_loader: FileBreadcrumbLoader | None = None,
        breadcrumb_rules: Sequence[BreadcrumbRule] | None = None,
    ) -> None:
        if not rules:
            raise ValueError("at least one rule is required")
        self._conn = conn
        self._loader = loader
        self._rules = list(rules)
        self._thresholds = thresholds_by_rule or {}
        self._clean_version = clean_version
        self._breadcrumb_loader = breadcrumb_loader
        self._breadcrumb_rules = list(breadcrumb_rules) if breadcrumb_rules else []

    def run(
        self,
        *,
        snapshot_id: str | None = None,
        max_replays: int | None = None,
    ) -> CleanStats:
        stats = CleanStats(started_at=_utcnow())
        rows = _fetch_unprocessed(
            self._conn, snapshot_id=snapshot_id, limit=max_replays
        )
        try:
            for row in rows:
                stats.replays_seen += 1
                self._clean_one(row, stats)
        finally:
            stats.completed_at = _utcnow()
        return stats

    def _clean_one(self, row: ReplayRow, stats: CleanStats) -> None:
        try:
            telemetry = self._loader.load(row)
        except TelemetryLoadError as telemetry_exc:
            stats.load_failures += 1
            if self._try_breadcrumb_path(row, stats, telemetry_exc):
                return
            # No breadcrumb fallback configured or it also failed.
            stats.errors.append(f"load replay={row.id}: {telemetry_exc}")
            _write_telemetry_unavailable(
                self._conn,
                replay_id=row.id,
                clean_version=self._clean_version,
                detail=str(telemetry_exc),
            )
            stats.replays_rejected += 1
            return
        try:
            results = run_rules(telemetry, self._rules, self._thresholds)
        except Exception as exc:  # noqa: BLE001
            stats.rule_exceptions += 1
            stats.errors.append(f"rules replay={row.id}: {exc}")
            _LOG.exception("rule exception on replay %d", row.id)
            return
        outcome = classify(results)
        _write_classification(
            self._conn,
            replay_id=row.id,
            outcome=outcome,
            clean_version=self._clean_version,
            signal_source="telemetry",
        )
        self._bump_stats(outcome, stats)

    def _try_breadcrumb_path(
        self,
        row: ReplayRow,
        stats: CleanStats,
        telemetry_exc: TelemetryLoadError,
    ) -> bool:
        """Attempt to classify using breadcrumbs. Returns True if the
        replay was classified (regardless of outcome); False if the
        breadcrumb path wasn't available and the caller should fall
        through to telemetry_unavailable rejection.
        """
        if self._breadcrumb_loader is None or not self._breadcrumb_rules:
            return False
        if not row.raw_artifact_path:
            return False
        try:
            breadcrumbs = self._breadcrumb_loader.load_by_path(row.raw_artifact_path)
        except BreadcrumbsLoadError:
            return False
        try:
            results = run_breadcrumb_rules(
                breadcrumbs, self._breadcrumb_rules, self._thresholds
            )
        except Exception as exc:  # noqa: BLE001
            stats.rule_exceptions += 1
            stats.errors.append(f"breadcrumb rules replay={row.id}: {exc}")
            _LOG.exception("breadcrumb rule exception on replay %d", row.id)
            return True  # replay handled (poorly) — don't fall through.
        outcome = classify(results)
        _write_classification(
            self._conn,
            replay_id=row.id,
            outcome=outcome,
            clean_version=self._clean_version,
            signal_source="breadcrumbs",
            extra_diagnostics={
                "telemetry_unavailable_reason": str(telemetry_exc)[:400],
            },
        )
        stats.breadcrumb_path_used += 1
        self._bump_stats(outcome, stats)
        return True

    @staticmethod
    def _bump_stats(outcome: ClassificationOutcome, stats: CleanStats) -> None:
        if outcome.status is CleanStatus.CLEAN:
            stats.replays_clean += 1
        elif outcome.status is CleanStatus.USABLE_WITH_WARNINGS:
            stats.replays_usable_with_warnings += 1
        else:
            stats.replays_rejected += 1


@dataclass
class CohortStats:
    started_at: datetime
    maps_processed: int = 0
    replays_assigned: int = 0
    completed_at: datetime | None = None
    per_map: list[MapCohortStats] = field(default_factory=list)

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "maps_processed": self.maps_processed,
            "replays_assigned": self.replays_assigned,
            "per_map_totals": {
                "performance": sum(m.performance for m in self.per_map),
                "intent": sum(m.intent for m in self.per_map),
                "robustness": sum(m.robustness for m in self.per_map),
            },
        }


def _eligible_by_map(
    conn: Connection,
    *,
    snapshot_id: str | None,
    map_ids: Iterable[int] | None,
) -> dict[int, list[tuple[int, int]]]:
    sql = (
        "SELECT map_id, id, finish_time_ms FROM replays "
        "WHERE clean_status IN ('clean','usable_with_warnings') "
        "AND finish_time_ms IS NOT NULL"
    )
    params: list[Any] = []
    if snapshot_id is not None:
        sql += " AND ingestion_snapshot = %s"
        params.append(snapshot_id)
    if map_ids is not None:
        ids = list(map_ids)
        if not ids:
            return {}
        placeholders = ",".join(["%s"] * len(ids))
        sql += f" AND map_id IN ({placeholders})"
        params.extend(ids)
    sql += " ORDER BY map_id, finish_time_ms"
    with cursor(conn) as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    grouped: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for map_id, replay_id, finish_ms in rows:
        grouped[int(map_id)].append((int(replay_id), int(finish_ms)))
    return grouped


def _write_cohorts(
    conn: Connection,
    *,
    replay_id: int,
    cohorts: frozenset[ReplayCohort],
) -> None:
    encoded = json.dumps(sorted(c.value for c in cohorts))
    with cursor(conn) as cur:
        cur.execute(
            "UPDATE replays SET cohort_membership=%s WHERE id=%s",
            (encoded, replay_id),
        )


class CohortAssignmentPipeline:
    def __init__(
        self,
        *,
        conn: Connection,
        config: CohortAssignmentConfig | None = None,
    ) -> None:
        self._conn = conn
        self._config = config or CohortAssignmentConfig()

    def run(
        self,
        *,
        snapshot_id: str | None = None,
        map_ids: Iterable[int] | None = None,
    ) -> CohortStats:
        stats = CohortStats(started_at=_utcnow())
        try:
            eligible = _eligible_by_map(
                self._conn, snapshot_id=snapshot_id, map_ids=map_ids
            )
            for map_id, rows in eligible.items():
                assignments = assign_cohorts_for_map(rows, config=self._config)
                for a in assignments:
                    _write_cohorts(self._conn, replay_id=a.replay_id, cohorts=a.cohorts)
                    stats.replays_assigned += 1
                self._conn.commit()
                stats.maps_processed += 1
                stats.per_map.append(summarize(map_id, assignments))
        finally:
            stats.completed_at = _utcnow()
        return stats
