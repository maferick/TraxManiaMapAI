"""Backfill TMX-sourced map metadata for an existing snapshot.

Refreshes the new `maps.track_value` and `maps.difficulty` columns (added
in migration 013) for rows that already exist in the DB but were ingested
before those fields were captured. Hits the v2 `/api/maps` list endpoint
once per map (`after=<id-1>&count=1&fields=...`) because there's no
working per-id detail endpoint on v2 for TM2020.

Rate-limited by the normal HttpClient (1 req/s with jitter + deadline),
so a full 999-map backfill takes ~17 minutes plus a little jitter.

Usage:
    python scripts/backfill_tmx_meta.py --snapshot 2026-04-scale-1k
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.ingestion.cache import ResponseCache  # noqa: E402
from src.ingestion.http import HttpClient  # noqa: E402
from src.ingestion.rate_limit import TokenBucket  # noqa: E402
from src.storage.mariadb import cursor, open_connection  # noqa: E402
from src.utils.config import load_config  # noqa: E402

_LOG = logging.getLogger(__name__)


def _fetch_map_ids(conn, snapshot: str) -> list[str]:
    with cursor(conn) as cur:
        cur.execute(
            "SELECT source_map_id FROM maps "
            "WHERE ingestion_snapshot=%s AND source_system='tmx' "
            "ORDER BY CAST(source_map_id AS UNSIGNED)",
            (snapshot,),
        )
        return [str(r[0]) for r in cur.fetchall()]


def _fetch_one(http: HttpClient, tmx_id: str) -> dict | None:
    """Return the TMX summary dict for exactly this id, or None if it's
    no longer listed on TMX.

    The v2 list endpoint accepts `id=<map_id>` as a direct filter —
    /api/maps/{id} and /api/maps?after=... don't work for single-id
    lookup on TM2020.
    """
    r = http.get(
        "/api/maps",
        params={
            "id": int(tmx_id),
            "count": 1,
            "fields": "MapId,TrackValue,Difficulty",
        },
        use_cache=False,
    )
    if r.status_code != 200:
        return None
    payload = r.json()
    results = payload.get("Results") if isinstance(payload, dict) else None
    if not results:
        return None
    entry = results[0]
    if str(entry.get("MapId")) != tmx_id:
        return None
    return entry


def _update_row(conn, *, source_map_id: str, snapshot: str, track_value, difficulty) -> None:
    with cursor(conn) as cur:
        cur.execute(
            "UPDATE maps SET track_value=%s, difficulty=%s "
            "WHERE source_system='tmx' AND source_map_id=%s "
            "AND ingestion_snapshot=%s",
            (track_value, difficulty, source_map_id, snapshot),
        )
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--limit", type=int, default=None, help="cap on how many rows to touch")
    p.add_argument("--progress-every", type=int, default=50)
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    tmx_cfg = cfg["ingestion"]["tmx"]
    retry_cfg = tmx_cfg.get("retry", {}) or {}
    http = HttpClient(
        base_url=tmx_cfg["base_url"],
        user_agent=tmx_cfg["user_agent"],
        rate_limiter=TokenBucket(rate_per_second=float(tmx_cfg.get("requests_per_second", 1.0))),
        cache=ResponseCache(Path(tmx_cfg["cache_dir"])),
        backoff_seconds=tuple(float(s) for s in retry_cfg.get("backoff_seconds", (2, 4, 8, 16))),
        timeout_seconds=float(tmx_cfg.get("timeout_seconds", 30.0)),
        max_total_retry_seconds=float(retry_cfg.get("max_total_retry_seconds", 120.0)),
    )

    conn = open_connection(cfg)
    try:
        ids = _fetch_map_ids(conn, args.snapshot)
    finally:
        pass

    if args.limit is not None:
        ids = ids[: args.limit]
    _LOG.info("backfilling %d maps in snapshot %s", len(ids), args.snapshot)

    updated = 0
    missing = 0
    errors = 0
    for i, tmx_id in enumerate(ids, start=1):
        try:
            entry = _fetch_one(http, tmx_id)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            _LOG.warning("fetch failed for %s: %s", tmx_id, exc)
            continue
        if entry is None:
            missing += 1
            continue
        _update_row(
            conn,
            source_map_id=tmx_id,
            snapshot=args.snapshot,
            track_value=entry.get("TrackValue"),
            difficulty=entry.get("Difficulty"),
        )
        updated += 1
        if i % args.progress_every == 0:
            _LOG.info(
                "%d/%d — updated=%d missing=%d errors=%d",
                i, len(ids), updated, missing, errors,
            )

    conn.close()
    _LOG.info("done — updated=%d missing=%d errors=%d", updated, missing, errors)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
