from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ingestion import ensure_snapshot
from src.replay.pipeline import ReplayRow, TelemetryLoadError
from src.replay.telemetry import ReplayTelemetry
from src.route import (
    GridClusterer,
    RouteExtractor,
    RoutePipeline,
    from_json,
)
from src.schema.replays import ReplayCohort
from tests.unit._telemetry_builders import make_telemetry

_TEST_SNAPSHOT = "route-it-test"


class _DictTelemetryLoader:
    def __init__(self, by_id: dict[int, ReplayTelemetry]) -> None:
        self._by_id = by_id

    def load(self, replay: ReplayRow) -> ReplayTelemetry:
        if replay.id not in self._by_id:
            raise TelemetryLoadError(f"no telemetry for replay {replay.id}")
        return self._by_id[replay.id]


def _cleanup(conn, snapshot_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE ra FROM route_artifacts ra JOIN maps m ON ra.map_id = m.id "
            "WHERE m.ingestion_snapshot = %s",
            (snapshot_id,),
        )
        cur.execute(
            "DELETE FROM stage_runs WHERE input_ref LIKE %s",
            (f"%{snapshot_id}%",),
        )
        cur.execute(
            "DELETE FROM replays WHERE ingestion_snapshot = %s", (snapshot_id,)
        )
        cur.execute(
            "DELETE FROM maps WHERE ingestion_snapshot = %s", (snapshot_id,)
        )
        cur.execute(
            "DELETE FROM ingestion_snapshots WHERE snapshot_id = %s",
            (snapshot_id,),
        )
    conn.commit()


@pytest.fixture
def seeded(db_conn):
    _cleanup(db_conn, _TEST_SNAPSHOT)
    ensure_snapshot(
        db_conn,
        snapshot_id=_TEST_SNAPSHOT,
        source_system="tmx",
        user_agent="test",
        rate_limit_rps=1000.0,
        resolved_config_hash="f" * 64,
        code_version="testsha",
    )
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO maps (
              source_system, source_map_id, ingestion_snapshot,
              parser_version, parse_status, created_by_version
            ) VALUES ('tmx', 'Mroute', %s, '0.0.0', 'success', '0.1.0')
            """,
            (_TEST_SNAPSHOT,),
        )
        map_id = int(cur.lastrowid)
    db_conn.commit()
    yield db_conn, map_id
    _cleanup(db_conn, _TEST_SNAPSHOT)


def _insert_clean_replay_with_cohort(
    conn,
    *,
    map_id: int,
    source_id: str,
    finish_ms: int,
    cohort_json: str,
    raw_path: str = "/fake/raw",
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO replays (
              source_system, source_replay_id, map_id, ingestion_snapshot,
              finish_time_ms, created_by_version, raw_artifact_path,
              raw_artifact_hash, clean_status, clean_version, cohort_membership
            ) VALUES (
              'tmx', %s, %s, %s, %s, '0.1.0', %s, %s,
              'clean', '0.1.0', %s
            )
            """,
            (
                source_id,
                map_id,
                _TEST_SNAPSHOT,
                finish_ms,
                raw_path,
                "a" * 64,
                cohort_json,
            ),
        )
        return int(cur.lastrowid)


def test_route_pipeline_end_to_end(seeded, tmp_path: Path) -> None:
    conn, map_id = seeded
    intent_cohort = json.dumps(["intent", "robustness"])
    perf_only = json.dumps(["performance"])

    intent_ids = [
        _insert_clean_replay_with_cohort(
            conn, map_id=map_id, source_id=f"i{i}", finish_ms=30_000 + i * 500,
            cohort_json=intent_cohort, raw_path=f"/fake/i{i}",
        )
        for i in range(4)
    ]
    # A performance-only replay; should be ignored because cohort filter is 'intent'.
    _insert_clean_replay_with_cohort(
        conn, map_id=map_id, source_id="perf", finish_ms=29_000,
        cohort_json=perf_only, raw_path="/fake/perf",
    )

    telemetries = {
        rid: make_telemetry(duration_ms=30_000 + i * 500, straight_speed_mps=30.0)
        for i, rid in enumerate(intent_ids)
    }
    loader = _DictTelemetryLoader(telemetries)

    extractor = RouteExtractor(
        clusterer=GridClusterer(cell_size=1.0),
        n_centerline_points=40,
    )
    artifacts_root = tmp_path / "artifacts"
    pipeline = RoutePipeline(
        conn=conn,
        loader=loader,
        extractor=extractor,
        artifacts_root=artifacts_root,
        route_version="1.0.0",
        created_by_version="0.1.0",
        clustering_method="grid",
        clustering_params={"cell_size": 1.0},
        cohort=ReplayCohort.INTENT,
        min_replays_per_map=3,
    )
    stats = pipeline.run(snapshot_id=_TEST_SNAPSHOT)
    assert stats.maps_seen == 1
    assert stats.routes_written == 1
    assert stats.routes_failed == 0

    with conn.cursor() as cur:
        cur.execute(
            "SELECT centerline_path, centerline_hash, clustering_method, "
            "replay_cohort, extraction_confidence "
            "FROM route_artifacts WHERE map_id=%s",
            (map_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    path, digest, method, cohort, confidence = rows[0]
    assert method == "grid"
    assert cohort == "intent"
    assert Path(path).is_file()
    # Disk round-trip
    payload = json.loads(Path(path).read_text())
    restored = from_json(payload)
    assert len(restored.centerline) == 40


def test_route_pipeline_skips_existing_row(seeded, tmp_path: Path) -> None:
    conn, map_id = seeded
    intent_cohort = json.dumps(["intent"])
    ids = [
        _insert_clean_replay_with_cohort(
            conn, map_id=map_id, source_id=f"r{i}", finish_ms=30_000 + i * 500,
            cohort_json=intent_cohort, raw_path=f"/fake/{i}",
        )
        for i in range(3)
    ]
    telemetries = {rid: make_telemetry(duration_ms=30_000 + i * 500) for i, rid in enumerate(ids)}

    def make_pipeline() -> RoutePipeline:
        return RoutePipeline(
            conn=conn,
            loader=_DictTelemetryLoader(telemetries),
            extractor=RouteExtractor(clusterer=GridClusterer(), n_centerline_points=30),
            artifacts_root=tmp_path / "artifacts",
            route_version="2.0.0",
            created_by_version="0.1.0",
            clustering_method="grid",
            clustering_params={"cell_size": 1.0},
            cohort=ReplayCohort.INTENT,
            min_replays_per_map=3,
        )

    first = make_pipeline().run(snapshot_id=_TEST_SNAPSHOT)
    second = make_pipeline().run(snapshot_id=_TEST_SNAPSHOT)
    assert first.routes_written == 1
    assert first.routes_skipped_existing == 0
    assert second.routes_written == 0
    assert second.routes_skipped_existing == 1


def test_route_pipeline_fails_when_too_few_replays(seeded, tmp_path: Path) -> None:
    conn, map_id = seeded
    intent_cohort = json.dumps(["intent"])
    _insert_clean_replay_with_cohort(
        conn, map_id=map_id, source_id="only", finish_ms=30_000,
        cohort_json=intent_cohort, raw_path="/fake/only",
    )
    pipeline = RoutePipeline(
        conn=conn,
        loader=_DictTelemetryLoader({}),
        extractor=RouteExtractor(clusterer=GridClusterer()),
        artifacts_root=tmp_path / "artifacts",
        route_version="3.0.0",
        created_by_version="0.1.0",
        clustering_method="grid",
        clustering_params={"cell_size": 1.0},
        cohort=ReplayCohort.INTENT,
        min_replays_per_map=3,
    )
    stats = pipeline.run(snapshot_id=_TEST_SNAPSHOT)
    assert stats.routes_written == 0
    assert stats.routes_failed == 1
