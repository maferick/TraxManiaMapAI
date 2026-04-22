"""Map-parse pipeline.

Reads unparsed maps from MariaDB, calls the GBX wrapper via
:class:`ParserClient` for each, and persists block placements into the
``block_placements`` table. Parse outcome is written back to the
``maps`` row's ``parse_status`` / ``parse_error_code`` /
``parse_error_detail`` fields. One transaction per map — blocks + status
update land together or not at all.

Grid and free blocks share a single table with an ``is_free``
discriminator (migration 010). ``is_free=False`` rows carry integer
``x/y/z``; ``is_free=True`` rows carry ``abs_x/abs_y/abs_z`` +
``yaw/pitch/roll`` floats.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from pymysql.connections import Connection

from src.parsers.base import ParserClient, ParseResult
from src.parsers.errors import ParseErrorCode, ParseStatus
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


_FAMILY_RE = re.compile(r"^([A-Z][a-z]+)")
_DIRECTION_TO_ROTATION: dict[str, int] = {
    "North": 0,
    "East": 1,
    "South": 2,
    "West": 3,
}


def extract_block_family(block_type: str) -> str:
    """Heuristic: take the leading CamelCase word as the family.

    ``DecoWallWaterBase`` → ``Deco``, ``PlatformTechLoopEnd`` →
    ``Platform``, ``RoadIceCurve2`` → ``Road``. Blocks whose name
    doesn't start with that pattern fall back to ``"Unknown"``.
    """
    if not isinstance(block_type, str):
        return "Unknown"
    match = _FAMILY_RE.match(block_type)
    return match.group(1) if match else "Unknown"


def direction_to_rotation(direction: str | None) -> int:
    if direction is None:
        return 0
    return _DIRECTION_TO_ROTATION.get(direction, 0)


@dataclass
class ParseStats:
    started_at: datetime
    maps_seen: int = 0
    maps_parsed: int = 0
    maps_failed_transient: int = 0
    maps_failed_permanent: int = 0
    maps_skipped: int = 0
    total_blocks_written: int = 0
    grid_blocks_written: int = 0
    free_blocks_written: int = 0
    errors: list[str] = field(default_factory=list)
    completed_at: datetime | None = None

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "maps_seen": self.maps_seen,
            "maps_parsed": self.maps_parsed,
            "maps_failed_transient": self.maps_failed_transient,
            "maps_failed_permanent": self.maps_failed_permanent,
            "maps_skipped": self.maps_skipped,
            "total_blocks_written": self.total_blocks_written,
            "grid_blocks_written": self.grid_blocks_written,
            "free_blocks_written": self.free_blocks_written,
            "error_count": len(self.errors),
        }


@dataclass(frozen=True)
class _UnparsedMap:
    id: int
    source_map_id: str
    raw_artifact_path: str
    raw_artifact_hash: str | None
    current_parse_status: str

    @property
    def needs_full_parse(self) -> bool:
        return self.current_parse_status != "success"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _fetch_unparsed(
    conn: Connection,
    *,
    snapshot_id: str | None,
    limit: int | None,
    retry_transient: bool,
) -> list[_UnparsedMap]:
    # A row is picked up if ANY of the following is true:
    #   parse_status = 'unparsed'                                 (fresh)
    #   parse_status = 'failed_transient' AND retry_transient     (retry)
    #   parse_status = 'success' AND decoration_parse_status = 'unparsed'
    #                                                             (retro-fill)
    # The third case covers maps that were parsed before migration 011
    # added scenery columns.
    clauses: list[str] = [
        "parse_status = 'unparsed'",
        "(parse_status = 'success' AND decoration_parse_status = 'unparsed')",
    ]
    if retry_transient:
        clauses.append("parse_status = 'failed_transient'")
    sql = (
        "SELECT id, source_map_id, raw_artifact_path, raw_artifact_hash, parse_status "
        f"FROM maps WHERE raw_artifact_path IS NOT NULL AND ({ ' OR '.join(clauses) })"
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
        _UnparsedMap(
            id=int(r[0]),
            source_map_id=str(r[1]),
            raw_artifact_path=str(r[2]),
            raw_artifact_hash=(str(r[3]) if r[3] is not None else None),
            current_parse_status=str(r[4]),
        )
        for r in rows
    ]


_INSERT_BLOCK_SQL = """
INSERT INTO block_placements (
    map_id, parser_version, block_family, block_type, variant,
    placement_index, x, y, z, rotation, flags,
    is_free, abs_x, abs_y, abs_z, yaw, pitch, roll,
    raw_blob, created_by_version, source_artifact_ids
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _block_row(
    *,
    map_id: int,
    parser_version: str,
    placement_index: int,
    block: Mapping[str, Any],
    created_by_version: str,
    source_artifact_ids: Mapping[str, str],
) -> tuple[Any, ...]:
    block_type = str(block.get("name") or "")
    variant = block.get("variant")
    variant_str = str(variant) if variant is not None else None
    rotation = direction_to_rotation(block.get("direction"))
    flags = block.get("flags")
    flags_int = int(flags) if flags is not None else None
    is_free = bool(block.get("placement") == "free")
    x = y = z = None
    abs_x = abs_y = abs_z = None
    yaw = pitch = roll = None
    if is_free:
        abs_x = _as_float(block.get("abs_x"))
        abs_y = _as_float(block.get("abs_y"))
        abs_z = _as_float(block.get("abs_z"))
        yaw = _as_float(block.get("yaw"))
        pitch = _as_float(block.get("pitch"))
        roll = _as_float(block.get("roll"))
    else:
        x = _as_int(block.get("x"))
        y = _as_int(block.get("y"))
        z = _as_int(block.get("z"))
    return (
        map_id,
        parser_version,
        extract_block_family(block_type),
        block_type,
        variant_str,
        placement_index,
        x,
        y,
        z,
        rotation,
        flags_int,
        int(is_free),
        abs_x,
        abs_y,
        abs_z,
        yaw,
        pitch,
        roll,
        json.dumps(dict(block)),
        created_by_version,
        json.dumps(dict(source_artifact_ids)),
    )


_INSERT_WAYPOINT_SQL = """
INSERT INTO map_checkpoints (
    map_id, parser_version, waypoint_index, tag, waypoint_order,
    block_name, placement, x, y, z, abs_x, abs_y, abs_z
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _waypoint_row(
    *,
    map_id: int,
    parser_version: str,
    waypoint_index: int,
    waypoint: Mapping[str, Any],
) -> tuple[Any, ...]:
    placement = "free" if waypoint.get("placement") == "free" else "grid"
    x = y = z = None
    abs_x = abs_y = abs_z = None
    if placement == "free":
        abs_x = _as_float(waypoint.get("abs_x"))
        abs_y = _as_float(waypoint.get("abs_y"))
        abs_z = _as_float(waypoint.get("abs_z"))
    else:
        x = _as_int(waypoint.get("x"))
        y = _as_int(waypoint.get("y"))
        z = _as_int(waypoint.get("z"))
    return (
        map_id,
        parser_version,
        waypoint_index,
        str(waypoint.get("tag") or ""),
        _as_int(waypoint.get("order")) or 0,
        str(waypoint.get("block_name") or ""),
        placement,
        x, y, z,
        abs_x, abs_y, abs_z,
    )


def _as_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_UPDATE_MAP_SUCCESS_SQL = """
UPDATE maps SET
    parse_status = 'success',
    parse_error_code = NULL,
    parse_error_detail = NULL,
    parser_version = %s,
    title = COALESCE(%s, title),
    author = COALESCE(%s, author),
    environment = COALESCE(%s, environment),
    has_items = COALESCE(%s, has_items),
    is_block_mode = COALESCE(%s, is_block_mode),
    mood = %s,
    decoration_id = %s,
    day_time_seconds = %s,
    dynamic_daylight = %s,
    scenery_item_count = %s,
    signpost_count = %s,
    scenery_standard_item_count = %s,
    scenery_custom_item_count = %s,
    has_custom_items = %s,
    decoration_parse_status = 'success'
WHERE id = %s
"""


# Scenery-only refresh. Used when parse_status is already 'success' but
# decoration_parse_status is 'unparsed' — i.e. retro-fill on maps
# ingested before migration 011. Doesn't touch parse_status or
# block_placements.
_UPDATE_MAP_SCENERY_SQL = """
UPDATE maps SET
    mood = %s,
    decoration_id = %s,
    day_time_seconds = %s,
    dynamic_daylight = %s,
    scenery_item_count = %s,
    signpost_count = %s,
    scenery_standard_item_count = %s,
    scenery_custom_item_count = %s,
    has_custom_items = %s,
    decoration_parse_status = 'success'
WHERE id = %s
"""


_UPDATE_MAP_FAILURE_SQL = """
UPDATE maps SET
    parse_status = %s,
    parse_error_code = %s,
    parse_error_detail = %s,
    parser_version = %s,
    decoration_parse_status = CASE
        WHEN decoration_parse_status = 'success' THEN decoration_parse_status
        ELSE 'failed'
    END
WHERE id = %s
"""


class MapParsePipeline:
    def __init__(
        self,
        *,
        conn: Connection,
        parser: ParserClient,
        parser_version: str,
        created_by_version: str,
    ) -> None:
        self._conn = conn
        self._parser = parser
        self._parser_version = parser_version
        self._created_by_version = created_by_version

    def run(
        self,
        *,
        snapshot_id: str | None = None,
        max_maps: int | None = None,
        retry_transient: bool = False,
    ) -> ParseStats:
        stats = ParseStats(started_at=_utcnow())
        try:
            maps = _fetch_unparsed(
                self._conn,
                snapshot_id=snapshot_id,
                limit=max_maps,
                retry_transient=retry_transient,
            )
            for row in maps:
                stats.maps_seen += 1
                self._process_one(row, stats)
        finally:
            stats.completed_at = _utcnow()
        return stats

    def _process_one(self, row: _UnparsedMap, stats: ParseStats) -> None:
        path = Path(row.raw_artifact_path)
        if not path.is_file():
            stats.maps_failed_transient += 1
            stats.errors.append(
                f"map={row.id}: raw_artifact_path missing on disk: {path}"
            )
            self._write_failure(
                row.id,
                status=ParseStatus.FAILED_TRANSIENT,
                error_code=ParseErrorCode.IO_ERROR,
                error_detail=f"artifact missing: {path}",
            )
            return

        result: ParseResult
        try:
            result = self._parser.parse_map(path)
        except Exception as exc:  # noqa: BLE001
            stats.maps_failed_transient += 1
            stats.errors.append(f"map={row.id} wrapper crash: {exc}")
            _LOG.exception("wrapper crashed on map %d", row.id)
            self._write_failure(
                row.id,
                status=ParseStatus.FAILED_TRANSIENT,
                error_code=ParseErrorCode.WRAPPER_CRASH,
                error_detail=f"{type(exc).__name__}: {exc}"[:2000],
            )
            return

        if result.status is not ParseStatus.SUCCESS or result.output is None:
            bucket = (
                "maps_failed_permanent"
                if result.status is ParseStatus.FAILED_PERMANENT
                else "maps_failed_transient"
            )
            setattr(stats, bucket, getattr(stats, bucket) + 1)
            stats.errors.append(
                f"map={row.id} parse failed: {result.error_code.value} "
                f"({result.error_detail or ''})"
            )
            self._write_failure(
                row.id,
                status=result.status,
                error_code=result.error_code,
                error_detail=result.error_detail,
            )
            return

        if row.needs_full_parse:
            self._write_success(row, result, stats)
        else:
            self._write_scenery_only(row, result, stats)

    def _scenery_params(
        self, scenery: Mapping[str, Any]
    ) -> tuple[Any, ...]:
        """Return the 9-tuple of scenery values shared by both UPDATE SQLs."""
        return (
            scenery.get("mood"),
            scenery.get("decoration_id"),
            _as_int(scenery.get("day_time_seconds")),
            _maybe_bool(scenery.get("dynamic_daylight")),
            _as_int(scenery.get("item_count")),
            _as_int(scenery.get("signpost_count")),
            _as_int(scenery.get("standard_item_count")),
            _as_int(scenery.get("custom_item_count")),
            _maybe_bool(scenery.get("has_custom_items")),
        )

    def _write_success(
        self,
        row: _UnparsedMap,
        result: ParseResult,
        stats: ParseStats,
    ) -> None:
        output = result.output or {}
        blocks = output.get("blocks") or []
        scenery = output.get("scenery") or {}
        waypoints = output.get("waypoints") or []
        source_ids = {
            "map": str(row.id),
            "raw_artifact_hash": row.raw_artifact_hash or "",
        }
        rows = [
            _block_row(
                map_id=row.id,
                parser_version=self._parser_version,
                placement_index=i,
                block=block,
                created_by_version=self._created_by_version,
                source_artifact_ids=source_ids,
            )
            for i, block in enumerate(blocks)
            if isinstance(block, Mapping)
        ]
        waypoint_rows = [
            _waypoint_row(
                map_id=row.id,
                parser_version=self._parser_version,
                waypoint_index=i,
                waypoint=wp,
            )
            for i, wp in enumerate(waypoints)
            if isinstance(wp, Mapping)
        ]

        try:
            with cursor(self._conn) as cur:
                if rows:
                    cur.executemany(_INSERT_BLOCK_SQL, rows)
                if waypoint_rows:
                    # Idempotent reparse: unique (map_id, parser_version,
                    # waypoint_index) would collide on re-run with the
                    # same parser_version. Delete then insert keeps the
                    # common case simple; multi-parser-version coexistence
                    # survives because we scope the delete.
                    cur.execute(
                        "DELETE FROM map_checkpoints "
                        "WHERE map_id = %s AND parser_version = %s",
                        (row.id, self._parser_version),
                    )
                    cur.executemany(_INSERT_WAYPOINT_SQL, waypoint_rows)
                cur.execute(
                    _UPDATE_MAP_SUCCESS_SQL,
                    (
                        self._parser_version,
                        output.get("title"),
                        output.get("author"),
                        output.get("environment"),
                        _maybe_bool(output.get("has_items")),
                        _maybe_bool(output.get("is_block_mode")),
                        *self._scenery_params(scenery),
                        row.id,
                    ),
                )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            self._conn.rollback()
            stats.maps_failed_transient += 1
            stats.errors.append(f"map={row.id} db insert failed: {exc}")
            _LOG.exception("block_placements insert failed on map %d", row.id)
            self._write_failure(
                row.id,
                status=ParseStatus.FAILED_TRANSIENT,
                error_code=ParseErrorCode.UNKNOWN,
                error_detail=f"db insert: {exc}"[:2000],
            )
            return

        stats.maps_parsed += 1
        grid = sum(1 for b in blocks if isinstance(b, Mapping) and b.get("placement") != "free")
        free = sum(1 for b in blocks if isinstance(b, Mapping) and b.get("placement") == "free")
        stats.total_blocks_written += grid + free
        stats.grid_blocks_written += grid
        stats.free_blocks_written += free

    def _write_scenery_only(
        self,
        row: _UnparsedMap,
        result: ParseResult,
        stats: ParseStats,
    ) -> None:
        """Retro-fill scenery on maps that were fully parsed before
        migration 011 added scenery columns. Skips block_placements
        entirely — those rows are already present.
        """
        output = result.output or {}
        scenery = output.get("scenery") or {}
        try:
            with cursor(self._conn) as cur:
                cur.execute(
                    _UPDATE_MAP_SCENERY_SQL,
                    (*self._scenery_params(scenery), row.id),
                )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            self._conn.rollback()
            stats.maps_failed_transient += 1
            stats.errors.append(f"map={row.id} scenery update failed: {exc}")
            _LOG.exception("scenery update failed on map %d", row.id)
            return
        stats.maps_parsed += 1

    def _write_failure(
        self,
        map_id: int,
        *,
        status: ParseStatus,
        error_code: ParseErrorCode,
        error_detail: str | None,
    ) -> None:
        with cursor(self._conn) as cur:
            cur.execute(
                _UPDATE_MAP_FAILURE_SQL,
                (
                    status.value,
                    error_code.value,
                    (error_detail or "")[:2000] or None,
                    self._parser_version,
                    map_id,
                ),
            )
        self._conn.commit()


def _maybe_bool(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0


# =============================================================================
# Replay parsing
# =============================================================================


@dataclass(frozen=True)
class _UnparsedReplay:
    id: int
    source_replay_id: str
    raw_artifact_path: str
    raw_artifact_hash: str | None


@dataclass
class ReplayParseStats:
    started_at: datetime
    replays_seen: int = 0
    replays_parsed: int = 0
    replays_failed_transient: int = 0
    replays_failed_permanent: int = 0
    sidecars_written: int = 0
    breadcrumbs_written: int = 0
    errors: list[str] = field(default_factory=list)
    completed_at: datetime | None = None

    def to_summary_json(self) -> dict[str, Any]:
        return {
            "replays_seen": self.replays_seen,
            "replays_parsed": self.replays_parsed,
            "replays_failed_transient": self.replays_failed_transient,
            "replays_failed_permanent": self.replays_failed_permanent,
            "sidecars_written": self.sidecars_written,
            "breadcrumbs_written": self.breadcrumbs_written,
            "error_count": len(self.errors),
        }


def _fetch_unparsed_replays(
    conn: Connection,
    *,
    snapshot_id: str | None,
    limit: int | None,
    retry_transient: bool,
) -> list[_UnparsedReplay]:
    statuses = ["unparsed"]
    if retry_transient:
        statuses.append("failed_transient")
    placeholders = ",".join(["%s"] * len(statuses))
    sql = (
        "SELECT id, source_replay_id, raw_artifact_path, raw_artifact_hash "
        f"FROM replays WHERE parse_status IN ({placeholders}) "
        "AND raw_artifact_path IS NOT NULL"
    )
    params: list[Any] = list(statuses)
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
        _UnparsedReplay(
            id=int(r[0]),
            source_replay_id=str(r[1]),
            raw_artifact_path=str(r[2]),
            raw_artifact_hash=(str(r[3]) if r[3] is not None else None),
        )
        for r in rows
    ]


_UPDATE_REPLAY_SUCCESS_SQL = """
UPDATE replays SET
    parse_status = 'success',
    parse_error_code = NULL,
    parse_error_detail = NULL,
    finish_time_ms = COALESCE(finish_time_ms, %s),
    player_login = COALESCE(player_login, %s),
    breadcrumbs_path = %s,
    breadcrumbs_hash = %s
WHERE id = %s
"""


_UPDATE_REPLAY_FAILURE_SQL = """
UPDATE replays SET
    parse_status = %s,
    parse_error_code = %s,
    parse_error_detail = %s
WHERE id = %s
"""


def _write_breadcrumbs_sidecar(
    raw_artifact_path: str, breadcrumbs: Mapping[str, Any]
) -> tuple[Path, str]:
    """Write `.breadcrumbs.json` next to the raw replay artifact. Returns
    (path, sha256_hex) so the caller can persist lineage. Atomic via
    tmp + replace. Bytes are hashed *before* the replace to avoid a
    race with concurrent parsers.
    """
    import os
    dest = Path(raw_artifact_path + ".breadcrumbs.json")
    payload = json.dumps(breadcrumbs, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(dest)
    return dest, digest


class ReplayParsePipeline:
    """Parse unparsed replays via the wrapper, write telemetry sidecars.

    For each replay:
      1. Invoke the wrapper on raw_artifact_path.
      2. On success, write ``<raw_artifact_path>.telemetry.json`` with
         the wrapper's output payload (the
         :class:`src.replay.pipeline.FileTelemetryLoader` expects
         exactly this sidecar).
      3. Update ``replays.parse_status`` + opportunistically backfill
         ``finish_time_ms`` / ``player_login`` if they're NULL.

    Like the map-parse pipeline, one transaction per replay. Sidecar
    writing is atomic (temp file + rename).
    """

    def __init__(
        self,
        *,
        conn: Connection,
        parser: ParserClient,
    ) -> None:
        self._conn = conn
        self._parser = parser

    def run(
        self,
        *,
        snapshot_id: str | None = None,
        max_replays: int | None = None,
        retry_transient: bool = False,
    ) -> ReplayParseStats:
        stats = ReplayParseStats(started_at=_utcnow())
        try:
            replays = _fetch_unparsed_replays(
                self._conn,
                snapshot_id=snapshot_id,
                limit=max_replays,
                retry_transient=retry_transient,
            )
            for row in replays:
                stats.replays_seen += 1
                self._process_one(row, stats)
        finally:
            stats.completed_at = _utcnow()
        return stats

    def _process_one(
        self, row: _UnparsedReplay, stats: ReplayParseStats
    ) -> None:
        path = Path(row.raw_artifact_path)
        if not path.is_file():
            stats.replays_failed_transient += 1
            stats.errors.append(
                f"replay={row.id}: raw_artifact_path missing on disk: {path}"
            )
            self._write_failure(
                row.id,
                status=ParseStatus.FAILED_TRANSIENT,
                error_code=ParseErrorCode.IO_ERROR,
                error_detail=f"artifact missing: {path}",
            )
            return
        try:
            result = self._parser.parse_replay(path)
        except Exception as exc:  # noqa: BLE001
            stats.replays_failed_transient += 1
            stats.errors.append(f"replay={row.id} wrapper crash: {exc}")
            _LOG.exception("wrapper crashed on replay %d", row.id)
            self._write_failure(
                row.id,
                status=ParseStatus.FAILED_TRANSIENT,
                error_code=ParseErrorCode.WRAPPER_CRASH,
                error_detail=f"{type(exc).__name__}: {exc}"[:2000],
            )
            return

        if result.status is not ParseStatus.SUCCESS or result.output is None:
            bucket = (
                "replays_failed_permanent"
                if result.status is ParseStatus.FAILED_PERMANENT
                else "replays_failed_transient"
            )
            setattr(stats, bucket, getattr(stats, bucket) + 1)
            stats.errors.append(
                f"replay={row.id} parse failed: {result.error_code.value}"
            )
            self._write_failure(
                row.id,
                status=result.status,
                error_code=result.error_code,
                error_detail=result.error_detail,
            )
            return

        self._write_success(row, result.output, stats)

    def _write_success(
        self,
        row: _UnparsedReplay,
        output: Mapping[str, Any],
        stats: ReplayParseStats,
    ) -> None:
        # Split breadcrumbs off before writing the telemetry sidecar so
        # the telemetry schema (ReplayTelemetry: samples non-empty)
        # isn't polluted with a field it doesn't model.
        output_mutable = dict(output)
        breadcrumbs = output_mutable.pop("breadcrumbs", None)

        sidecar_path = Path(row.raw_artifact_path + ".telemetry.json")
        breadcrumbs_path: Path | None = None
        breadcrumbs_hash: str | None = None
        try:
            import os
            tmp = sidecar_path.with_suffix(sidecar_path.suffix + ".tmp")
            sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(output_mutable, fh, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(sidecar_path)
            stats.sidecars_written += 1

            if isinstance(breadcrumbs, Mapping):
                breadcrumbs_path, breadcrumbs_hash = _write_breadcrumbs_sidecar(
                    row.raw_artifact_path, breadcrumbs
                )
                stats.breadcrumbs_written += 1
        except Exception as exc:  # noqa: BLE001
            stats.replays_failed_transient += 1
            stats.errors.append(
                f"replay={row.id} sidecar write failed: {exc}"
            )
            self._write_failure(
                row.id,
                status=ParseStatus.FAILED_TRANSIENT,
                error_code=ParseErrorCode.IO_ERROR,
                error_detail=f"sidecar write: {exc}"[:2000],
            )
            return

        finish_time_ms = output.get("finish_time_ms")
        player_login = output.get("player_login")
        try:
            with cursor(self._conn) as cur:
                cur.execute(
                    _UPDATE_REPLAY_SUCCESS_SQL,
                    (
                        _as_int(finish_time_ms),
                        player_login,
                        str(breadcrumbs_path) if breadcrumbs_path else None,
                        breadcrumbs_hash,
                        row.id,
                    ),
                )
            self._conn.commit()
            stats.replays_parsed += 1
        except Exception as exc:  # noqa: BLE001
            self._conn.rollback()
            stats.replays_failed_transient += 1
            stats.errors.append(f"replay={row.id} db update failed: {exc}")
            _LOG.exception("replay status update failed on %d", row.id)

    def _write_failure(
        self,
        replay_id: int,
        *,
        status: ParseStatus,
        error_code: ParseErrorCode,
        error_detail: str | None,
    ) -> None:
        with cursor(self._conn) as cur:
            cur.execute(
                _UPDATE_REPLAY_FAILURE_SQL,
                (
                    status.value,
                    error_code.value,
                    (error_detail or "")[:2000] or None,
                    replay_id,
                ),
            )
        self._conn.commit()
