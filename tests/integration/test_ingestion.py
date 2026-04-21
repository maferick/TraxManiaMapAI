from __future__ import annotations

from pathlib import Path

import responses

from src.ingestion import (
    ArtifactStore,
    HttpClient,
    MapIngestor,
    ResponseCache,
    TmxClient,
    TokenBucket,
    close_stage_run,
    ensure_snapshot,
    open_stage_run,
)


@responses.activate
def test_end_to_end_map_ingestion(db_conn, tmp_path: Path, test_snapshot: str) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/maps",
        match=[responses.matchers.query_param_matcher({"limit": "50"})],
        json={
            "items": [
                {
                    "id": "100",
                    "title": "Strong tech",
                    "author": "alice",
                    "environment": "Stadium",
                    "tags": ["tech"],
                    "length_ms": 52000,
                    "award_count": 123,
                    "rating": 92.5,
                    "popularity": 5000,
                    "has_items": False,
                    "is_block_mode": True,
                },
                {
                    "id": "101",
                    "title": "Mediocre tech",
                    "author": "bob",
                    "tags": ["tech"],
                },
            ],
            "cursor": None,
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://tmx.test/maps/100/download",
        body=b"fake-gbx-bytes-100",
        status=200,
    )
    responses.add(
        responses.GET,
        "https://tmx.test/maps/101/download",
        body=b"fake-gbx-bytes-101",
        status=200,
    )

    ensure_snapshot(
        db_conn,
        snapshot_id=test_snapshot,
        source_system="tmx",
        user_agent="test-ua/0.1",
        rate_limit_rps=1000.0,
        resolved_config_hash="f" * 64,
        code_version="testsha",
    )

    http = HttpClient(
        base_url="https://tmx.test",
        user_agent="test-ua/0.1",
        rate_limiter=TokenBucket(rate_per_second=1000.0),
        cache=ResponseCache(tmp_path / "cache"),
        sleep=lambda _: None,
    )
    client = TmxClient(http, page_size=50)
    store = ArtifactStore(tmp_path / "artifacts")

    stage_run_id = open_stage_run(
        db_conn,
        stage="ingest_maps",
        stage_version="0.1.0",
        resolved_config_hash="f" * 64,
        code_version="testsha",
        input_ref=f"snapshot={test_snapshot}",
    )
    ingestor = MapIngestor(
        tmx=client,
        conn=db_conn,
        artifact_store=store,
        snapshot_id=test_snapshot,
        parser_version="0.0.0",
        created_by_version="0.1.0",
    )
    stats = ingestor.run()
    close_stage_run(
        db_conn,
        stage_run_id,
        status="success",
        output_summary=stats.to_summary_json(),
    )

    assert stats.maps_seen == 2
    assert stats.maps_inserted == 2
    assert stats.artifacts_downloaded == 2
    assert stats.errors == []

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT source_map_id, title, author, raw_artifact_hash "
            "FROM maps WHERE ingestion_snapshot=%s ORDER BY source_map_id",
            (test_snapshot,),
        )
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["100", "101"]
    assert rows[0][1] == "Strong tech"
    assert rows[1][2] == "bob"
    # Artifact hashes were recorded.
    assert all(len(r[3]) == 64 for r in rows)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, output_summary FROM stage_runs WHERE id=%s",
            (stage_run_id,),
        )
        stage = cur.fetchone()
    assert stage[0] == "success"


@responses.activate
def test_second_run_is_idempotent(db_conn, tmp_path: Path, test_snapshot: str) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/maps",
        json={
            "items": [{"id": "200", "title": "Resumable", "tags": []}],
            "cursor": None,
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://tmx.test/maps/200/download",
        body=b"gbx-200",
        status=200,
    )

    ensure_snapshot(
        db_conn,
        snapshot_id=test_snapshot,
        source_system="tmx",
        user_agent="ua",
        rate_limit_rps=1000.0,
        resolved_config_hash="a" * 64,
        code_version="sha",
    )

    def _run() -> int:
        http = HttpClient(
            base_url="https://tmx.test",
            user_agent="ua",
            rate_limiter=TokenBucket(rate_per_second=1000.0),
            cache=ResponseCache(tmp_path / "cache"),
            sleep=lambda _: None,
        )
        ingestor = MapIngestor(
            tmx=TmxClient(http, page_size=50),
            conn=db_conn,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            snapshot_id=test_snapshot,
            parser_version="0.0.0",
            created_by_version="0.1.0",
        )
        return ingestor.run().maps_inserted

    first = _run()
    second = _run()
    assert first == 1
    assert second == 0  # already present -> UPSERT -> updated, not inserted

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM maps WHERE ingestion_snapshot=%s",
            (test_snapshot,),
        )
        (count,) = cur.fetchone()
    assert count == 1
