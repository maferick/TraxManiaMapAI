from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.ingestion import ensure_snapshot
from src.replay import (
    CohortAssignmentConfig,
    CohortAssignmentPipeline,
    ReplayCleanPipeline,
    ReplayRow,
    default_rules,
)
from src.replay.pipeline import TelemetryLoadError
from src.replay.telemetry import ReplayTelemetry
from src.schema.replays import CleanStatus, ReplayCohort
from tests.unit._telemetry_builders import make_telemetry, with_samples

_TEST_SNAPSHOT = "replay-it-test"


class _DictTelemetryLoader:
    """In-memory loader keyed by replay id for tests."""

    def __init__(self, by_id: dict[int, ReplayTelemetry]) -> None:
        self._by_id = by_id

    def load(self, replay: ReplayRow) -> ReplayTelemetry:
        if replay.id not in self._by_id:
            raise TelemetryLoadError(f"no telemetry for replay {replay.id}")
        return self._by_id[replay.id]


def _cleanup_replay_state(conn, snapshot_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM stage_runs WHERE input_ref LIKE %s",
            (f"%{snapshot_id}%",),
        )
        cur.execute("DELETE FROM replays WHERE ingestion_snapshot = %s", (snapshot_id,))
        cur.execute(
            "DELETE FROM maps WHERE ingestion_snapshot = %s", (snapshot_id,)
        )
        cur.execute(
            "DELETE FROM ingestion_snapshots WHERE snapshot_id = %s", (snapshot_id,)
        )
    conn.commit()


@pytest.fixture
def seeded(db_conn):
    _cleanup_replay_state(db_conn, _TEST_SNAPSHOT)
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
            ) VALUES ('tmx', 'M1', %s, '0.0.0', 'success', '0.1.0')
            """,
            (_TEST_SNAPSHOT,),
        )
        map_id = cur.lastrowid
    db_conn.commit()
    yield db_conn, int(map_id)
    _cleanup_replay_state(db_conn, _TEST_SNAPSHOT)


def _insert_replay(
    conn, *, map_id: int, source_id: str, finish_ms: int | None, raw_path: str | None = None
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO replays (
              source_system, source_replay_id, map_id, ingestion_snapshot,
              finish_time_ms, created_by_version, raw_artifact_path
            ) VALUES ('tmx', %s, %s, %s, %s, '0.1.0', %s)
            """,
            (source_id, map_id, _TEST_SNAPSHOT, finish_ms, raw_path),
        )
        return int(cur.lastrowid)


def test_clean_pipeline_classifies_replays(seeded) -> None:
    conn, map_id = seeded
    clean_id = _insert_replay(
        conn, map_id=map_id, source_id="clean", finish_ms=30_000, raw_path="/fake/r1"
    )
    warn_id = _insert_replay(
        conn, map_id=map_id, source_id="warn", finish_ms=32_000, raw_path="/fake/r2"
    )
    reject_id = _insert_replay(
        conn, map_id=map_id, source_id="reject", finish_ms=None, raw_path="/fake/r3"
    )

    loader_map = {
        clean_id: make_telemetry(duration_ms=30_000, straight_speed_mps=30.0),
        warn_id: make_telemetry(
            duration_ms=32_000, straight_speed_mps=30.0, restart_indices=(100,)
        ),
        reject_id: make_telemetry(
            duration_ms=200, sample_rate_hz=50, finished=False  # too-few samples
        ),
    }
    pipeline = ReplayCleanPipeline(
        conn=conn,
        loader=_DictTelemetryLoader(loader_map),
        rules=default_rules(),
        thresholds_by_rule={},
        clean_version="0.1.0",
    )
    stats = pipeline.run(snapshot_id=_TEST_SNAPSHOT)
    assert stats.replays_seen == 3
    assert stats.replays_clean == 1
    assert stats.replays_usable_with_warnings == 1
    assert stats.replays_rejected == 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_replay_id, clean_status, clean_version, clean_diagnostics "
            "FROM replays WHERE ingestion_snapshot=%s ORDER BY source_replay_id",
            (_TEST_SNAPSHOT,),
        )
        rows = cur.fetchall()
    by_source = {r[0]: r for r in rows}
    assert by_source["clean"][1] == CleanStatus.CLEAN.value
    assert by_source["warn"][1] == CleanStatus.USABLE_WITH_WARNINGS.value
    assert by_source["reject"][1] == CleanStatus.REJECTED.value
    for source in ("clean", "warn", "reject"):
        assert by_source[source][2] == "0.1.0"
        diag = json.loads(by_source[source][3])
        assert "rules" in diag
        assert diag["status"] == by_source[source][1]


def test_load_failure_marks_rejected(seeded) -> None:
    conn, map_id = seeded
    no_path_id = _insert_replay(
        conn, map_id=map_id, source_id="no_path", finish_ms=30_000, raw_path=None
    )
    pipeline = ReplayCleanPipeline(
        conn=conn,
        loader=_DictTelemetryLoader({}),
        rules=default_rules(),
        thresholds_by_rule={},
        clean_version="0.1.0",
    )
    stats = pipeline.run(snapshot_id=_TEST_SNAPSHOT)
    assert stats.load_failures == 1
    assert stats.replays_rejected == 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT clean_status, clean_diagnostics FROM replays WHERE id=%s",
            (no_path_id,),
        )
        row = cur.fetchone()
    assert row[0] == CleanStatus.REJECTED.value
    diag = json.loads(row[1])
    assert "telemetry_unavailable" in diag["triggered"]


def test_cohort_pipeline_assigns_memberships(seeded) -> None:
    conn, map_id = seeded
    # Three clean replays with different finish times
    ids = []
    for i, ms in enumerate([20_000, 22_000, 25_000]):
        rid = _insert_replay(
            conn, map_id=map_id, source_id=f"fast{i}", finish_ms=ms, raw_path=f"/f/{i}"
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE replays SET clean_status='clean', clean_version='0.1.0' "
                "WHERE id=%s",
                (rid,),
            )
        conn.commit()
        ids.append(rid)

    pipeline = CohortAssignmentPipeline(
        conn=conn, config=CohortAssignmentConfig(small_map_n=10)
    )
    stats = pipeline.run(snapshot_id=_TEST_SNAPSHOT)
    assert stats.maps_processed == 1
    assert stats.replays_assigned == 3

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, cohort_membership FROM replays WHERE ingestion_snapshot=%s "
            "ORDER BY id",
            (_TEST_SNAPSHOT,),
        )
        rows = cur.fetchall()
    for rid, cohort_json in rows:
        cohorts = json.loads(cohort_json)
        # small_map_n=10 triggers the "assign all cohorts" branch for n=3.
        assert set(cohorts) == {c.value for c in ReplayCohort}


def test_cohort_ignores_unprocessed_replays(seeded) -> None:
    conn, map_id = seeded
    # One clean, one unprocessed — cohort pipeline should only count the clean one.
    clean_id = _insert_replay(
        conn, map_id=map_id, source_id="c1", finish_ms=20_000, raw_path="/a"
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE replays SET clean_status='clean' WHERE id=%s", (clean_id,)
        )
    conn.commit()
    _insert_replay(
        conn, map_id=map_id, source_id="u1", finish_ms=21_000, raw_path="/b"
    )

    pipeline = CohortAssignmentPipeline(conn=conn)
    stats = pipeline.run(snapshot_id=_TEST_SNAPSHOT)
    assert stats.replays_assigned == 1
