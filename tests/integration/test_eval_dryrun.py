from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.benchmarks.manifest import load as load_benchmark
from src.constraints import ConstraintGraphPipeline
from src.evaluation import (
    AdjacencyGraphEvaluator,
    RouteCoverageEvaluator,
    StructuralEvaluator,
)
from src.evaluation.dryrun import DryRunRunner, render_markdown
from src.ingestion import ensure_snapshot
from src.storage.neo4j_adapter import apply_pending as apply_neo4j_pending, open_driver


_TEST_SNAPSHOT = "2026-04-evaltest"
_BENCH_STRONG = "eval-test-strong"
_BENCH_MEDIOCRE = "eval-test-mediocre"
_PARSER_VERSION = "0.0.0"


def _cleanup_mariadb(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM evaluation_artifacts WHERE map_id IN "
            "(SELECT id FROM maps WHERE ingestion_snapshot = %s)",
            (_TEST_SNAPSHOT,),
        )
        cur.execute(
            "DELETE FROM stage_runs WHERE input_ref LIKE %s",
            (f"%{_TEST_SNAPSHOT}%",),
        )
        cur.execute(
            "DELETE FROM route_artifacts WHERE map_id IN "
            "(SELECT id FROM maps WHERE ingestion_snapshot = %s)",
            (_TEST_SNAPSHOT,),
        )
        cur.execute(
            "DELETE FROM block_placements WHERE map_id IN "
            "(SELECT id FROM maps WHERE ingestion_snapshot = %s)",
            (_TEST_SNAPSHOT,),
        )
        cur.execute(
            "DELETE FROM maps WHERE ingestion_snapshot = %s", (_TEST_SNAPSHOT,)
        )
        cur.execute(
            "DELETE FROM ingestion_snapshots WHERE snapshot_id = %s",
            (_TEST_SNAPSHOT,),
        )
    conn.commit()


def _cleanup_neo4j(driver) -> None:
    with driver.session() as s:
        s.run(
            "MATCH (p:ProcessedMap) WHERE p.snapshot_id = $snapshot DETACH DELETE p",
            snapshot=_TEST_SNAPSHOT,
        ).consume()
        s.run(
            "MATCH ()-[r:ADJACENT_TO]->() "
            "WHERE r.first_seen_snapshot = $snapshot OR r.last_seen_snapshot = $snapshot "
            "DELETE r",
            snapshot=_TEST_SNAPSHOT,
        ).consume()
        s.run("MATCH (b:Block) WHERE NOT (b)--() DELETE b").consume()


@pytest.fixture
def neo4j_driver(config):
    try:
        driver = open_driver(config)
        driver.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j not reachable: {exc}")
    apply_neo4j_pending(driver)
    yield driver
    driver.close()


def _insert_map(conn, source_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO maps (
              source_system, source_map_id, ingestion_snapshot,
              parser_version, parse_status, created_by_version
            ) VALUES ('tmx', %s, %s, %s, 'success', '0.1.0')
            """,
            (source_id, _TEST_SNAPSHOT, _PARSER_VERSION),
        )
        return int(cur.lastrowid)


def _insert_placement(conn, map_id: int, *, x: int, y: int, z: int, idx: int, type: str = "straight") -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO block_placements (
              map_id, parser_version, block_family, block_type, variant,
              x, y, z, placement_index, created_by_version, source_artifact_ids
            ) VALUES (%s, %s, 'tech', %s, NULL, %s, %s, %s, %s, '0.1.0', '{}')
            """,
            (map_id, _PARSER_VERSION, type, x, y, z, idx),
        )
    conn.commit()


def _insert_route_artifact(conn, map_id: int, *, confidence: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO route_artifacts (
              map_id, route_version, centerline_path, centerline_hash,
              clustering_method, clustering_params, replay_cohort,
              extraction_confidence, diagnostics, created_by_version,
              source_artifact_ids
            ) VALUES (%s, '1.0.0', '/fake/cl.json', %s, 'grid', '{}', 'intent',
                      %s, '{"n_replays": 5}', '0.1.0', '{}')
            """,
            (map_id, "a" * 64, round(confidence, 4)),
        )
    conn.commit()


def _write_manifest(
    tmp_path: Path, *, benchmark_id: str, category: str, entries: list[dict]
) -> Path:
    data = {
        "schema_version": 1,
        "benchmark_id": benchmark_id,
        "version": 1,
        "category": category,
        "ingestion_snapshot": _TEST_SNAPSHOT,
        "released_at": "2026-04-21",
        "author": "test@example.com",
        "rationale": (
            "Integration test manifest exercising the dry-run over "
            "hand-seeded maps."
        ),
        "entries": entries,
    }
    path = tmp_path / f"{benchmark_id}-v1.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_dryrun_produces_artifacts_and_report(
    db_conn, neo4j_driver, tmp_path: Path
) -> None:
    # --- Cleanup pre-existing state
    _cleanup_mariadb(db_conn)
    _cleanup_neo4j(neo4j_driver)
    ensure_snapshot(
        db_conn,
        snapshot_id=_TEST_SNAPSHOT,
        source_system="tmx",
        user_agent="test",
        rate_limit_rps=1000.0,
        resolved_config_hash="f" * 64,
        code_version="testsha",
    )
    try:
        # Strong map: a 3-in-a-row chain (connected, no orphans).
        strong_id = _insert_map(db_conn, "strong-1")
        _insert_placement(db_conn, strong_id, x=0, y=0, z=0, idx=0)
        _insert_placement(db_conn, strong_id, x=1, y=0, z=0, idx=1, type="curve")
        _insert_placement(db_conn, strong_id, x=2, y=0, z=0, idx=2)
        _insert_route_artifact(db_conn, strong_id, confidence=0.90)

        # Mediocre map: one connected pair + one orphan.
        mediocre_id = _insert_map(db_conn, "mediocre-1")
        _insert_placement(db_conn, mediocre_id, x=0, y=0, z=0, idx=0)
        _insert_placement(db_conn, mediocre_id, x=1, y=0, z=0, idx=1, type="curve")
        _insert_placement(db_conn, mediocre_id, x=10, y=10, z=10, idx=2, type="ramp")
        _insert_route_artifact(db_conn, mediocre_id, confidence=0.50)

        # Build the constraint graph so adjacency_graph evaluator has something to query.
        ConstraintGraphPipeline(
            mariadb=db_conn, neo4j_driver=neo4j_driver, stage_version="0.1.0"
        ).run(snapshot_id=_TEST_SNAPSHOT)

        # Write benchmark manifests pointing at each map.
        strong_manifest_path = _write_manifest(
            tmp_path,
            benchmark_id=_BENCH_STRONG,
            category="strong_tech",
            entries=[
                {
                    "map_id": "strong-1",
                    "content_hash": "a" * 64,
                    "role": "primary",
                    "label": {"hand_curated": True},
                }
            ],
        )
        mediocre_manifest_path = _write_manifest(
            tmp_path,
            benchmark_id=_BENCH_MEDIOCRE,
            category="mediocre_tech",
            entries=[
                {
                    "map_id": "mediocre-1",
                    "content_hash": "b" * 64,
                    "role": "primary",
                    "label": {"hand_curated": True},
                }
            ],
        )
        manifests = [
            load_benchmark(strong_manifest_path),
            load_benchmark(mediocre_manifest_path),
        ]

        evaluators = [
            StructuralEvaluator(db_conn, parser_version=_PARSER_VERSION),
            AdjacencyGraphEvaluator(
                db_conn, neo4j_driver, parser_version=_PARSER_VERSION
            ),
            RouteCoverageEvaluator(db_conn),
        ]

        runner = DryRunRunner(
            conn=db_conn,
            evaluators=evaluators,
            benchmark_manifests=manifests,
            community_sample_size=0,
        )
        report = runner.run()

        # --- Assertions on the in-memory report
        assert len(report.maps) == 2
        assert report.errors == []
        strong_results = report.results[strong_id]
        mediocre_results = report.results[mediocre_id]
        assert len(strong_results) == 3
        assert len(mediocre_results) == 3

        # Strong map has no orphans: structural_score == 1.0
        structural_strong = next(
            r for r in strong_results if r.evaluator_name == "structural"
        )
        assert structural_strong.structural_score == pytest.approx(1.0)

        # Mediocre map: 1 orphan out of 3 blocks → structural_score ≈ 2/3
        structural_med = next(
            r for r in mediocre_results if r.evaluator_name == "structural"
        )
        assert structural_med.structural_score == pytest.approx(2 / 3)

        # Route coverage surfaces extraction_confidence
        rc_strong = next(
            r for r in strong_results if r.evaluator_name == "route_coverage"
        )
        assert rc_strong.drivability_score == pytest.approx(0.90)

        # --- Persisted evaluation_artifacts rows
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT map_id, evaluator_name, structural_score, drivability_score "
                "FROM evaluation_artifacts "
                "WHERE map_id IN (%s, %s) ORDER BY map_id, evaluator_name",
                (strong_id, mediocre_id),
            )
            rows = cur.fetchall()
        by_evaluator = {(int(r[0]), r[1]): r for r in rows}
        assert (strong_id, "structural") in by_evaluator
        assert (strong_id, "adjacency_graph") in by_evaluator
        assert (strong_id, "route_coverage") in by_evaluator

        # --- Markdown renders without errors and pins the right versions
        markdown = render_markdown(report)
        assert "# Evaluator Dry-Run Report v1" in markdown
        assert "structural@0.1.0" in markdown
        assert "eval-test-strong-v1" in markdown
        assert "eval-test-mediocre-v1" in markdown
        # Strong has score 1.0, mediocre 0.67 → structural AUC = 1.0
        assert "1.0000" in markdown
    finally:
        _cleanup_mariadb(db_conn)
        _cleanup_neo4j(neo4j_driver)


def test_dryrun_with_no_benchmarks_still_renders(db_conn, neo4j_driver, tmp_path: Path) -> None:
    _cleanup_mariadb(db_conn)
    _cleanup_neo4j(neo4j_driver)
    ensure_snapshot(
        db_conn,
        snapshot_id=_TEST_SNAPSHOT,
        source_system="tmx",
        user_agent="test",
        rate_limit_rps=1000.0,
        resolved_config_hash="f" * 64,
        code_version="testsha",
    )
    try:
        mid = _insert_map(db_conn, "sample-1")
        _insert_placement(db_conn, mid, x=0, y=0, z=0, idx=0)
        _insert_placement(db_conn, mid, x=1, y=0, z=0, idx=1, type="curve")

        runner = DryRunRunner(
            conn=db_conn,
            evaluators=[StructuralEvaluator(db_conn, parser_version=_PARSER_VERSION)],
            benchmark_manifests=(),
            community_sample_size=10,
            community_snapshot_id=_TEST_SNAPSHOT,
        )
        report = runner.run()
        assert len(report.maps) == 1
        md = render_markdown(report)
        assert "No benchmark sets were run" in md
        assert "No maps in categories" in md  # separation section graceful
    finally:
        _cleanup_mariadb(db_conn)
        _cleanup_neo4j(neo4j_driver)
