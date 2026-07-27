"""Ingest in-game ghost telemetry captures into the corpus.

Captures are produced by ``tools/capture_replay_telemetry.py`` driving
the AIReplayTelemetry plugin. Each one is a real driven run of a map's
author validation ghost, which is a guaranteed successful finish, so it
lands in ``replays`` alongside TMX-sourced replays rather than in a
side table.

Linking back to the map
-----------------------
``maps`` has no ``map_uid`` column; its natural key is
``(source_system, source_map_id, ingestion_snapshot)`` and its content
key is ``raw_artifact_hash``. Corpus artifacts are stored under their
sha256, so the staged map filename carries that hash and the capture
records it in ``extra.map_file``. That is the join, and a capture
without it cannot be attached to anything.

Idempotence
-----------
Re-ingesting the same capture updates the existing row rather than
creating a second one, so a partial batch can be re-run safely. Every
pipeline stage in this repo is required to be resumable.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.replay.telemetry import from_dict as telemetry_from_dict

_LOG = logging.getLogger(__name__)

SOURCE_SYSTEM = "openplanet_ghost"
DEFAULT_CAPTURE_SOURCE = "map_author_ghost"


@dataclass
class IngestStats:
    seen: int = 0
    ingested: int = 0
    updated: int = 0
    unmatched: int = 0
    invalid: int = 0
    unmatched_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seen": self.seen,
            "ingested": self.ingested,
            "updated": self.updated,
            "unmatched": self.unmatched,
            "invalid": self.invalid,
        }


def _artifact_hash_from_map_file(map_file: str | None) -> str | None:
    """Pull the corpus artifact sha256 out of a staged map path.

    Staged maps are ``<sha256>.Map.Gbx``. Anything else (a hand-named
    map, a generated pilot map) has no corpus counterpart and returns
    None rather than a guess.
    """
    if not map_file:
        return None
    name = map_file.replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".Map.Gbx", ".map.gbx"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    name = name.strip().lower()
    if len(name) == 64 and all(c in "0123456789abcdef" for c in name):
        return name
    return None


def ensure_snapshot(conn, snapshot: str, *, version: str) -> None:
    """Create the ingestion_snapshots row for a capture batch if absent.

    A capture run is its own ingestion event with its own provenance, so
    it gets its own snapshot rather than borrowing the TMX one. Reusing
    a scrape's snapshot id would make the captures look like they came
    from TMX, and `replays.ingestion_snapshot` is a foreign key, so the
    row has to exist before any capture can be attached.

    Rate limit is 0: this reads a local game client, not a rate-limited
    community API.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM ingestion_snapshots WHERE snapshot_id = %s",
            (snapshot,),
        )
        if cur.fetchone():
            return
        cur.execute(
            """
            INSERT INTO ingestion_snapshots (
                snapshot_id, source_system, started_at, completed_at,
                user_agent, rate_limit_rps, resolved_config_hash,
                code_version, notes
            ) VALUES (%s,%s,NOW(6),NOW(6),%s,0,%s,%s,%s)
            """,
            (
                snapshot,
                SOURCE_SYSTEM,
                f"AIReplayTelemetry/{version}",
                hashlib.sha256(version.encode()).hexdigest(),
                version,
                "in-game ghost playback capture; positions only, "
                "no inputs (a ghost exposes no input surface)",
            ),
        )
    conn.commit()
    _LOG.info("created ingestion snapshot %s", snapshot)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest_directory(
    conn,
    telemetry_dir: Path,
    *,
    snapshot: str,
    version: str,
    dry_run: bool = False,
) -> IngestStats:
    """Ingest every ``*.telemetry.json`` under ``telemetry_dir``."""
    stats = IngestStats()
    files = sorted(telemetry_dir.glob("*.telemetry.json"))
    _LOG.info("found %d telemetry artifact(s) in %s", len(files), telemetry_dir)
    if files and not dry_run:
        ensure_snapshot(conn, snapshot, version=version)

    for path in files:
        stats.seen += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            telemetry = telemetry_from_dict(payload)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            _LOG.error("%s: not valid telemetry (%s)", path.name, exc)
            stats.invalid += 1
            continue

        extra = telemetry.extra or {}
        art_hash = _artifact_hash_from_map_file(extra.get("map_file"))
        if art_hash is None:
            _LOG.warning(
                "%s: no corpus artifact hash in extra.map_file (%r), skipping",
                path.name, extra.get("map_file"),
            )
            stats.unmatched += 1
            stats.unmatched_files.append(path.name)
            continue

        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM maps WHERE raw_artifact_hash = %s LIMIT 1",
                (art_hash,),
            )
            row = cur.fetchone()
        if not row:
            _LOG.warning(
                "%s: artifact %s not in maps, skipping", path.name, art_hash[:12]
            )
            stats.unmatched += 1
            stats.unmatched_files.append(path.name)
            continue
        map_id = row[0] if isinstance(row, (tuple, list)) else row["id"]

        if dry_run:
            stats.ingested += 1
            continue

        telemetry_hash = _sha256(path)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO replays (
                    source_system, source_replay_id, map_id,
                    ingestion_snapshot, player_display_name, finish_time_ms,
                    parse_status, openplanet_telemetry_path,
                    openplanet_telemetry_hash, capture_source,
                    created_by_version
                ) VALUES (%s,%s,%s,%s,%s,%s,'success',%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    map_id = VALUES(map_id),
                    player_display_name = VALUES(player_display_name),
                    finish_time_ms = VALUES(finish_time_ms),
                    parse_status = VALUES(parse_status),
                    openplanet_telemetry_path = VALUES(openplanet_telemetry_path),
                    openplanet_telemetry_hash = VALUES(openplanet_telemetry_hash),
                    capture_source = VALUES(capture_source)
                """,
                (
                    SOURCE_SYSTEM,
                    telemetry.source_replay_id,
                    map_id,
                    snapshot,
                    extra.get("ghost_nickname"),
                    telemetry.finish_time_ms,
                    str(path),
                    telemetry_hash,
                    extra.get("ghost_source") or DEFAULT_CAPTURE_SOURCE,
                    version,
                ),
            )
            # rowcount is 1 for a fresh insert and 2 for an update that
            # actually changed something, which is how MariaDB reports
            # ON DUPLICATE KEY UPDATE.
            if cur.rowcount and cur.rowcount > 1:
                stats.updated += 1
            else:
                stats.ingested += 1
        conn.commit()

    _LOG.info(
        "ingested %d, updated %d, unmatched %d, invalid %d (of %d)",
        stats.ingested, stats.updated, stats.unmatched, stats.invalid,
        stats.seen,
    )
    return stats
