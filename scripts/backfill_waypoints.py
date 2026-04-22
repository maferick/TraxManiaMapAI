"""Backfill map_checkpoints rows for already-parsed maps.

Scope: existing maps have parse_status='success' under
parser_version='0.1.0' and their block_placements are already
populated. Migration 015 added map_checkpoints (empty). This script
invokes the wrapper on each map's raw artifact, extracts the
`waypoints[]` array, and bulk-inserts into map_checkpoints under the
same parser_version.

Avoids a full re-parse + parser_version bump because:
- block placements are bit-identical between runs (wrapper output is
  deterministic for a given raw_artifact + parser_version)
- re-INSERTing 600k+ block_placements rows is pure thrash; it would
  collide on uq_block_placement and force either a DELETE+INSERT or
  a version bump
- waypoints are additive metadata; adding them doesn't invalidate
  anything downstream

Idempotent: if a map already has rows in map_checkpoints for this
parser_version, skipped (no work, no crash).

Usage:
    python scripts/backfill_waypoints.py
    python scripts/backfill_waypoints.py --limit 50   # smoke
    python scripts/backfill_waypoints.py --parser-version 0.1.0
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.parsers.pipeline import _INSERT_WAYPOINT_SQL, _waypoint_row  # noqa: E402
from src.parsers.subprocess_parser import SubprocessParser  # noqa: E402
from src.parsers.errors import ParseStatus  # noqa: E402
from src.storage.mariadb import cursor, open_connection  # noqa: E402
from src.utils.config import load_config  # noqa: E402

_LOG = logging.getLogger(__name__)


def _maps_missing_waypoints(conn, parser_version: str, limit: int | None) -> list[tuple[int, str]]:
    """Return ``(map_id, raw_artifact_path)`` for success-parsed maps
    at the given parser_version that don't yet have map_checkpoints rows.

    A map with zero waypoints in the wrapper output (no checkpoints —
    rare but possible) can't be distinguished from an un-backfilled
    map by the schema alone. We err on the side of "re-invoke the
    wrapper" for those; the wrapper is fast enough that spending
    ~1-2s per edge case is fine on 999 maps.
    """
    sql = (
        "SELECT m.id, m.raw_artifact_path "
        "FROM maps m "
        "LEFT JOIN map_checkpoints mc "
        "  ON mc.map_id = m.id AND mc.parser_version = %s "
        "WHERE m.parse_status = 'success' "
        "  AND m.parser_version = %s "
        "  AND m.raw_artifact_path IS NOT NULL "
        "  AND mc.map_id IS NULL "
        "ORDER BY m.id"
    )
    params = [parser_version, parser_version]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(int(limit))
    with cursor(conn) as cur:
        cur.execute(sql, tuple(params))
        return [(int(r[0]), str(r[1])) for r in cur.fetchall()]


def _insert_waypoints(
    conn,
    *,
    map_id: int,
    parser_version: str,
    waypoints: list[dict],
) -> int:
    """Insert the given waypoints. Returns the row count written."""
    if not waypoints:
        return 0
    rows = [
        _waypoint_row(
            map_id=map_id,
            parser_version=parser_version,
            waypoint_index=i,
            waypoint=wp,
        )
        for i, wp in enumerate(waypoints)
        if isinstance(wp, dict)
    ]
    with cursor(conn) as cur:
        cur.executemany(_INSERT_WAYPOINT_SQL, rows)
    conn.commit()
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=None)
    p.add_argument(
        "--parser-version",
        default="0.1.0",
        help="parser_version to backfill under (must match existing block_placements rows)",
    )
    p.add_argument("--limit", type=int, default=None, help="cap on maps to touch")
    p.add_argument("--progress-every", type=int, default=25)
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    gbx_cfg = (cfg.get("parsers") or {}).get("gbx") or {}
    executable = Path(
        gbx_cfg.get("executable")
        or "./parsers/gbx-wrapper/bin/Release/net8.0/GbxWrapper"
    )
    if not executable.is_file():
        _LOG.error("wrapper executable not found: %r", str(executable))
        return 2

    parser = SubprocessParser(
        executable=executable,
        parser_version=args.parser_version,
        timeout_seconds=float(gbx_cfg.get("timeout_seconds", 30.0)),
    )

    conn = open_connection(cfg)
    try:
        targets = _maps_missing_waypoints(conn, args.parser_version, args.limit)
    finally:
        pass
    _LOG.info("backfilling waypoints for %d map(s)", len(targets))

    maps_done = 0
    maps_zero_waypoints = 0
    maps_failed = 0
    waypoint_rows = 0

    for i, (map_id, raw_path) in enumerate(targets, start=1):
        artifact = REPO_ROOT / raw_path
        if not artifact.is_file():
            _LOG.warning("map %d: artifact missing on disk at %s", map_id, artifact)
            maps_failed += 1
            continue
        result = parser.parse_map(artifact)
        if result.status is not ParseStatus.SUCCESS or result.output is None:
            _LOG.warning(
                "map %d: parse failed %s: %s",
                map_id, result.error_code.value,
                (result.error_detail or "")[:200],
            )
            maps_failed += 1
            continue
        waypoints = result.output.get("waypoints") or []
        try:
            n = _insert_waypoints(
                conn,
                map_id=map_id,
                parser_version=args.parser_version,
                waypoints=waypoints,
            )
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            _LOG.warning("map %d: insert failed: %s", map_id, exc)
            maps_failed += 1
            continue
        maps_done += 1
        if n == 0:
            maps_zero_waypoints += 1
        waypoint_rows += n
        if i % args.progress_every == 0:
            _LOG.info(
                "%d/%d — maps_done=%d zero_wp=%d failed=%d rows=%d",
                i, len(targets),
                maps_done, maps_zero_waypoints, maps_failed, waypoint_rows,
            )

    conn.close()
    _LOG.info(
        "done — maps_done=%d zero_waypoints=%d failed=%d waypoint_rows=%d",
        maps_done, maps_zero_waypoints, maps_failed, waypoint_rows,
    )
    return 0 if maps_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
