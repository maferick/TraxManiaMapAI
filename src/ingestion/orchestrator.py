"""Map-ingestion orchestrator.

Wires :class:`TmxClient`, DB access, and the artifact store into a
single resumable pass. Resumability comes from two properties that
compose:

- the HTTP cache (see ``cache.py``) makes re-listing TMX free
- the UPSERT on ``maps`` is idempotent on the natural key
  ``(source_system, source_map_id, ingestion_snapshot)``

A stage_run row is opened at start and updated at end. Snapshots are
created if they don't exist; a second invocation against the same
snapshot_id resumes rather than overwriting.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from pymysql.connections import Connection

from src.storage.mariadb import cursor

from .artifacts import ArtifactStore
from .http import HttpError
from .tmx import TmxClient, TmxMapSummary, TmxReplaySummary

_LOG = logging.getLogger(__name__)

_MAP_SOURCE_SYSTEM = "tmx"


@dataclass
class IngestionStats:
    started_at: datetime
    snapshot_id: str
    source_system: str
    maps_seen: int = 0
    maps_inserted: int = 0
    maps_updated: int = 0
    artifacts_downloaded: int = 0
    artifacts_failed: int = 0
    summary_failures: int = 0
    completed_at: datetime | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> int | None:
        if self.completed_at is None:
            return None
        return int((self.completed_at - self.started_at).total_seconds() * 1000)

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "maps_seen": self.maps_seen,
            "maps_inserted": self.maps_inserted,
            "maps_updated": self.maps_updated,
            "artifacts_downloaded": self.artifacts_downloaded,
            "artifacts_failed": self.artifacts_failed,
            "summary_failures": self.summary_failures,
            "error_count": len(self.errors),
        }


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def ensure_snapshot(
    conn: Connection,
    *,
    snapshot_id: str,
    source_system: str,
    user_agent: str,
    rate_limit_rps: float,
    resolved_config_hash: str,
    code_version: str,
    notes: str | None = None,
) -> None:
    """Insert the snapshot row if it doesn't exist. Idempotent."""
    with cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO ingestion_snapshots (
                snapshot_id, source_system, started_at, user_agent,
                rate_limit_rps, resolved_config_hash, code_version, notes
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE snapshot_id = snapshot_id
            """,
            (
                snapshot_id,
                source_system,
                _utcnow(),
                user_agent,
                Decimal(str(rate_limit_rps)),
                resolved_config_hash,
                code_version,
                notes,
            ),
        )
    conn.commit()


def open_stage_run(
    conn: Connection,
    *,
    stage: str,
    stage_version: str,
    resolved_config_hash: str,
    code_version: str,
    input_ref: str,
) -> int:
    with cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO stage_runs (
                stage, stage_version, started_at, resolved_config_hash,
                code_version, input_ref, status
            ) VALUES (%s, %s, %s, %s, %s, %s, 'running')
            """,
            (stage, stage_version, _utcnow(), resolved_config_hash, code_version, input_ref),
        )
        stage_run_id = cur.lastrowid
    conn.commit()
    return int(stage_run_id)


def close_stage_run(
    conn: Connection,
    stage_run_id: int,
    *,
    status: str,
    output_summary: Mapping[str, Any] | None,
    error_taxonomy_code: str | None = None,
    error_message: str | None = None,
) -> None:
    completed_at = _utcnow()
    with cursor(conn) as cur:
        cur.execute(
            """
            UPDATE stage_runs
               SET completed_at = %s,
                   duration_ms = TIMESTAMPDIFF(MICROSECOND, started_at, %s) DIV 1000,
                   output_summary = %s,
                   status = %s,
                   error_taxonomy_code = %s,
                   error_message = %s
             WHERE id = %s
            """,
            (
                completed_at,
                completed_at,
                json.dumps(dict(output_summary)) if output_summary is not None else None,
                status,
                error_taxonomy_code,
                error_message,
                stage_run_id,
            ),
        )
    conn.commit()


def _upsert_map(
    conn: Connection,
    *,
    summary: TmxMapSummary,
    snapshot_id: str,
    parser_version: str,
    created_by_version: str,
) -> tuple[int, bool]:
    """UPSERT a maps row. Returns (map_id, inserted_new)."""
    with cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO maps (
                source_system, source_map_id, ingestion_snapshot,
                title, author, environment, style_tags_raw,
                length_estimate_ms, award_count, average_rating, popularity_metric,
                has_items, is_block_mode,
                parser_version, parse_status,
                created_by_version
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'unparsed', %s)
            ON DUPLICATE KEY UPDATE
                title              = VALUES(title),
                author             = VALUES(author),
                environment        = VALUES(environment),
                style_tags_raw     = VALUES(style_tags_raw),
                length_estimate_ms = VALUES(length_estimate_ms),
                award_count        = VALUES(award_count),
                average_rating     = VALUES(average_rating),
                popularity_metric  = VALUES(popularity_metric),
                has_items          = VALUES(has_items),
                is_block_mode      = VALUES(is_block_mode)
            """,
            (
                _MAP_SOURCE_SYSTEM,
                summary.tmx_id,
                snapshot_id,
                summary.title,
                summary.author,
                summary.environment,
                json.dumps(summary.style_tags_raw),
                summary.length_estimate_ms,
                summary.award_count,
                (
                    Decimal(str(summary.average_rating))
                    if summary.average_rating is not None
                    else None
                ),
                summary.popularity_metric,
                int(summary.has_items),
                int(summary.is_block_mode),
                parser_version,
                created_by_version,
            ),
        )
        affected = cur.rowcount
        cur.execute(
            "SELECT id FROM maps WHERE source_system=%s AND source_map_id=%s "
            "AND ingestion_snapshot=%s",
            (_MAP_SOURCE_SYSTEM, summary.tmx_id, snapshot_id),
        )
        row = cur.fetchone()
    conn.commit()
    if row is None:
        raise RuntimeError(
            f"UPSERT succeeded but SELECT returned nothing for {summary.tmx_id}"
        )
    # rowcount: 1 = inserted new, 2 = updated existing (MariaDB convention)
    inserted_new = affected == 1
    return int(row[0]), inserted_new


def _record_artifact(
    conn: Connection,
    *,
    map_id: int,
    content_hash: str,
    artifact_path: str,
) -> None:
    with cursor(conn) as cur:
        cur.execute(
            "UPDATE maps SET raw_artifact_hash=%s, raw_artifact_path=%s WHERE id=%s",
            (content_hash, artifact_path, map_id),
        )
    conn.commit()


class MapIngestor:
    def __init__(
        self,
        *,
        tmx: TmxClient,
        conn: Connection,
        artifact_store: ArtifactStore,
        snapshot_id: str,
        parser_version: str,
        created_by_version: str,
        max_maps: int | None = None,
        download_artifacts: bool = True,
        random_count: int | None = None,
    ) -> None:
        self._tmx = tmx
        self._conn = conn
        self._store = artifact_store
        self._snapshot_id = snapshot_id
        self._parser_version = parser_version
        self._created_by_version = created_by_version
        self._max_maps = max_maps
        self._download_artifacts = download_artifacts
        self._random_count = random_count

    def _summaries(self):
        if self._random_count is not None:
            return self._tmx.iter_random_summaries(count=self._random_count)
        return self._tmx.iter_map_summaries()

    def run(self) -> IngestionStats:
        stats = IngestionStats(
            started_at=_utcnow(),
            snapshot_id=self._snapshot_id,
            source_system=_MAP_SOURCE_SYSTEM,
        )
        try:
            for summary in self._summaries():
                if self._max_maps is not None and stats.maps_seen >= self._max_maps:
                    break
                stats.maps_seen += 1
                try:
                    map_id, inserted = _upsert_map(
                        self._conn,
                        summary=summary,
                        snapshot_id=self._snapshot_id,
                        parser_version=self._parser_version,
                        created_by_version=self._created_by_version,
                    )
                except Exception as exc:  # noqa: BLE001
                    stats.summary_failures += 1
                    stats.errors.append(f"upsert {summary.tmx_id}: {exc}")
                    _LOG.error("UPSERT failed for %s: %s", summary.tmx_id, exc)
                    continue
                if inserted:
                    stats.maps_inserted += 1
                else:
                    stats.maps_updated += 1

                if self._download_artifacts:
                    self._fetch_and_store_artifact(summary.tmx_id, map_id, stats)
        finally:
            stats.completed_at = _utcnow()
        return stats

    def _fetch_and_store_artifact(
        self,
        tmx_id: str,
        map_id: int,
        stats: IngestionStats,
    ) -> None:
        try:
            response = self._tmx.download_map_artifact(tmx_id)
        except HttpError as exc:
            stats.artifacts_failed += 1
            stats.errors.append(f"download {tmx_id}: {exc}")
            _LOG.warning("artifact download failed for %s: %s", tmx_id, exc)
            return
        if response.status_code != 200:
            stats.artifacts_failed += 1
            stats.errors.append(
                f"download {tmx_id}: HTTP {response.status_code}"
            )
            return
        digest, path = self._store.write(response.content)
        _record_artifact(
            self._conn, map_id=map_id, content_hash=digest, artifact_path=str(path)
        )
        stats.artifacts_downloaded += 1


# =============================================================================
# Replay ingestion
# =============================================================================


_REPLAY_SOURCE_SYSTEM = "tmx"


@dataclass
class ReplayIngestionStats:
    started_at: datetime
    maps_seen: int = 0
    maps_skipped_no_map_row: int = 0
    maps_with_no_replays: int = 0
    replays_seen: int = 0
    replays_inserted: int = 0
    replays_updated: int = 0
    artifacts_downloaded: int = 0
    artifacts_failed: int = 0
    errors: list[str] = field(default_factory=list)
    completed_at: datetime | None = None

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "maps_seen": self.maps_seen,
            "maps_skipped_no_map_row": self.maps_skipped_no_map_row,
            "maps_with_no_replays": self.maps_with_no_replays,
            "replays_seen": self.replays_seen,
            "replays_inserted": self.replays_inserted,
            "replays_updated": self.replays_updated,
            "artifacts_downloaded": self.artifacts_downloaded,
            "artifacts_failed": self.artifacts_failed,
            "error_count": len(self.errors),
        }


def _upsert_replay(
    conn: Connection,
    *,
    summary: TmxReplaySummary,
    map_id: int,
    snapshot_id: str,
    created_by_version: str,
) -> tuple[int, bool]:
    """UPSERT a replays row. Returns (replay_db_id, inserted_new)."""
    rank_metadata = {
        "position": summary.position,
        "beaten": summary.beaten,
        "stunt_score": summary.stunt_score,
        "respawns": summary.respawns,
        "player_model": summary.player_model,
        "exe_build": summary.exe_build,
        "replay_uploaded_at": summary.replay_uploaded_at,
    }
    with cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO replays (
                source_system, source_replay_id, map_id, ingestion_snapshot,
                player_display_name, finish_time_ms, rank_metadata,
                created_by_version
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                map_id              = VALUES(map_id),
                player_display_name = VALUES(player_display_name),
                finish_time_ms      = VALUES(finish_time_ms),
                rank_metadata       = VALUES(rank_metadata)
            """,
            (
                _REPLAY_SOURCE_SYSTEM,
                summary.replay_id,
                map_id,
                snapshot_id,
                summary.player_display_name,
                summary.finish_time_ms,
                json.dumps(rank_metadata),
                created_by_version,
            ),
        )
        affected = cur.rowcount
        cur.execute(
            "SELECT id FROM replays WHERE source_system=%s AND source_replay_id=%s "
            "AND ingestion_snapshot=%s",
            (_REPLAY_SOURCE_SYSTEM, summary.replay_id, snapshot_id),
        )
        row = cur.fetchone()
    conn.commit()
    if row is None:
        raise RuntimeError(
            f"UPSERT succeeded but SELECT returned nothing for replay {summary.replay_id}"
        )
    inserted_new = affected == 1
    return int(row[0]), inserted_new


def _record_replay_artifact(
    conn: Connection,
    *,
    replay_id: int,
    content_hash: str,
    artifact_path: str,
) -> None:
    with cursor(conn) as cur:
        cur.execute(
            "UPDATE replays SET raw_artifact_hash=%s, raw_artifact_path=%s WHERE id=%s",
            (content_hash, artifact_path, replay_id),
        )
    conn.commit()


class ReplayIngestor:
    """Fetch replays per map and persist them into the ``replays`` table.

    Input is a list of ``(db_map_id, source_map_id)`` pairs — the
    caller decides which maps get replays (e.g. top-awards subset of
    a snapshot). The ingestor calls the list endpoint once per map
    (at the rate limiter's pace), UPSERTs replay rows, and downloads
    each artifact to the content-addressed store.
    """

    def __init__(
        self,
        *,
        tmx: TmxClient,
        conn: Connection,
        artifact_store: ArtifactStore,
        snapshot_id: str,
        created_by_version: str,
        per_map: int | None = None,
        download_artifacts: bool = True,
    ) -> None:
        self._tmx = tmx
        self._conn = conn
        self._store = artifact_store
        self._snapshot_id = snapshot_id
        self._created_by_version = created_by_version
        self._per_map = per_map
        self._download_artifacts = download_artifacts

    def run(
        self, map_refs: list[tuple[int, str]]
    ) -> ReplayIngestionStats:
        stats = ReplayIngestionStats(started_at=_utcnow())
        try:
            for db_map_id, source_map_id in map_refs:
                stats.maps_seen += 1
                self._process_map(db_map_id, source_map_id, stats)
        finally:
            stats.completed_at = _utcnow()
        return stats

    def _process_map(
        self,
        db_map_id: int,
        source_map_id: str,
        stats: ReplayIngestionStats,
    ) -> None:
        try:
            iterator = self._tmx.iter_replays_for_map(
                source_map_id, amount=self._per_map
            )
            summaries = list(iterator)
        except HttpError as exc:
            stats.errors.append(f"list map={source_map_id}: {exc}")
            _LOG.warning("replay list failed for %s: %s", source_map_id, exc)
            return
        if not summaries:
            stats.maps_with_no_replays += 1
            return
        if self._per_map is not None:
            summaries = summaries[: self._per_map]
        for summary in summaries:
            stats.replays_seen += 1
            try:
                replay_db_id, inserted = _upsert_replay(
                    self._conn,
                    summary=summary,
                    map_id=db_map_id,
                    snapshot_id=self._snapshot_id,
                    created_by_version=self._created_by_version,
                )
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(
                    f"upsert replay={summary.replay_id}: {exc}"
                )
                _LOG.error(
                    "UPSERT replay failed for %s: %s", summary.replay_id, exc
                )
                continue
            if inserted:
                stats.replays_inserted += 1
            else:
                stats.replays_updated += 1
            if self._download_artifacts:
                self._fetch_and_store_artifact(
                    summary.replay_id, replay_db_id, stats
                )

    def _fetch_and_store_artifact(
        self,
        replay_source_id: str,
        replay_db_id: int,
        stats: ReplayIngestionStats,
    ) -> None:
        try:
            response = self._tmx.download_replay_artifact(replay_source_id)
        except HttpError as exc:
            stats.artifacts_failed += 1
            stats.errors.append(
                f"download replay={replay_source_id}: {exc}"
            )
            return
        if response.status_code != 200:
            stats.artifacts_failed += 1
            stats.errors.append(
                f"download replay={replay_source_id}: HTTP {response.status_code}"
            )
            return
        digest, path = self._store.write(response.content)
        _record_replay_artifact(
            self._conn,
            replay_id=replay_db_id,
            content_hash=digest,
            artifact_path=str(path),
        )
        stats.artifacts_downloaded += 1
