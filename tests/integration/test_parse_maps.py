"""Integration test for the map-parse stage.

Re-uses the 10 real maps downloaded by the earlier TMX v2 smoke test
(snapshot ``2026-04-live-sample``). If that snapshot is absent (first
run on a fresh DB), the test is skipped rather than attempting a
live TMX download inside the test suite.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.parsers import MapParsePipeline, SubprocessParser

_TEST_SNAPSHOT = "2026-04-live-sample"
_WRAPPER_PATH = Path("parsers/gbx-wrapper/bin/Release/net8.0/GbxWrapper")


@pytest.fixture
def wrapper_ready() -> None:
    if not _WRAPPER_PATH.is_file():
        pytest.skip(
            f"GBX wrapper binary missing at {_WRAPPER_PATH}; "
            "build it with `dotnet build parsers/gbx-wrapper -c Release`"
        )


@pytest.fixture
def live_sample_rows(db_conn) -> list[tuple[int, str, str]]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, source_map_id, raw_artifact_path FROM maps "
            "WHERE ingestion_snapshot = %s ORDER BY id",
            (_TEST_SNAPSHOT,),
        )
        rows = [(int(r[0]), str(r[1]), str(r[2])) for r in cur.fetchall()]
    if not rows:
        pytest.skip(
            f"snapshot {_TEST_SNAPSHOT!r} has no maps; run the TMX v2 "
            "smoke test first (`ingest-maps --random N`)"
        )
    return rows


def _reset_parse_state(db_conn, map_ids: list[int]) -> None:
    if not map_ids:
        return
    placeholders = ",".join(["%s"] * len(map_ids))
    with db_conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM block_placements WHERE map_id IN ({placeholders})",
            tuple(map_ids),
        )
        cur.execute(
            f"UPDATE maps SET parse_status='unparsed', parse_error_code=NULL, "
            f"parse_error_detail=NULL WHERE id IN ({placeholders})",
            tuple(map_ids),
        )
    db_conn.commit()


def test_parse_live_sample_end_to_end(
    db_conn, wrapper_ready, live_sample_rows
) -> None:
    ids = [r[0] for r in live_sample_rows]
    _reset_parse_state(db_conn, ids)

    parser = SubprocessParser(
        executable=_WRAPPER_PATH,
        parser_version="0.1.0",
        timeout_seconds=30.0,
    )
    pipeline = MapParsePipeline(
        conn=db_conn,
        parser=parser,
        parser_version="0.1.0",
        created_by_version="0.1.0",
    )
    stats = pipeline.run(snapshot_id=_TEST_SNAPSHOT)

    assert stats.maps_seen == len(ids)
    # Every real map downloaded via the TMX v2 smoke test should parse.
    assert stats.maps_parsed == len(ids), (
        f"parse regressions on live sample: {stats.errors}"
    )
    assert stats.total_blocks_written > 0

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT parse_status, COUNT(*) FROM maps "
            "WHERE ingestion_snapshot=%s GROUP BY parse_status",
            (_TEST_SNAPSHOT,),
        )
        by_status = {row[0]: int(row[1]) for row in cur.fetchall()}
    assert by_status.get("success") == len(ids)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*), SUM(is_free = 0), SUM(is_free = 1) "
            "FROM block_placements WHERE map_id IN (%s)"
            % ",".join(["%s"] * len(ids)),
            tuple(ids),
        )
        total, grid, free = cur.fetchone()
    assert int(total) == stats.total_blocks_written
    assert int(grid or 0) == stats.grid_blocks_written
    assert int(free or 0) == stats.free_blocks_written


def test_idempotency_skips_already_parsed(
    db_conn, wrapper_ready, live_sample_rows
) -> None:
    # Previous test leaves the maps parsed; running the pipeline again
    # without --retry-transient should process zero new maps.
    pipeline = MapParsePipeline(
        conn=db_conn,
        parser=SubprocessParser(
            executable=_WRAPPER_PATH,
            parser_version="0.1.0",
            timeout_seconds=30.0,
        ),
        parser_version="0.1.0",
        created_by_version="0.1.0",
    )
    stats = pipeline.run(snapshot_id=_TEST_SNAPSHOT)
    assert stats.maps_seen == 0
    assert stats.maps_parsed == 0
