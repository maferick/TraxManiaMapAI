from __future__ import annotations

import pytest

from src.constraints import ConstraintGraphPipeline
from src.ingestion import ensure_snapshot
from src.storage.neo4j_adapter import apply_pending as apply_neo4j_pending, open_driver


_TEST_SNAPSHOT = "constraints-it-test"


def _cleanup_mariadb(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM stage_runs WHERE input_ref LIKE %s",
            (f"%{_TEST_SNAPSHOT}%",),
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
        # Delete only the test snapshot's traces. The graph itself is shared.
        s.run(
            "MATCH (p:ProcessedMap) WHERE p.snapshot_id = $snapshot DETACH DELETE p",
            snapshot=_TEST_SNAPSHOT,
        ).consume()
        # Clear test adjacency edges — any touched by this snapshot.
        s.run(
            "MATCH ()-[r:ADJACENT_TO]->() "
            "WHERE r.first_seen_snapshot = $snapshot OR r.last_seen_snapshot = $snapshot "
            "DELETE r",
            snapshot=_TEST_SNAPSHOT,
        ).consume()
        s.run(
            "MATCH (b:Block) WHERE NOT (b)--() DELETE b"
        ).consume()


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


@pytest.fixture
def seeded(db_conn, neo4j_driver):
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
    yield db_conn, neo4j_driver
    _cleanup_mariadb(db_conn)
    _cleanup_neo4j(neo4j_driver)


def _insert_map(conn, source_id: str, parser_version: str = "0.0.0") -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO maps (
              source_system, source_map_id, ingestion_snapshot,
              parser_version, parse_status, created_by_version
            ) VALUES ('tmx', %s, %s, %s, 'success', '0.1.0')
            """,
            (source_id, _TEST_SNAPSHOT, parser_version),
        )
        return int(cur.lastrowid)


def _insert_placement(
    conn,
    *,
    map_id: int,
    parser_version: str,
    idx: int,
    x: int,
    y: int,
    z: int,
    family: str = "tech",
    type: str = "straight",
    variant: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO block_placements (
              map_id, parser_version, block_family, block_type, variant,
              x, y, z, placement_index, created_by_version, source_artifact_ids
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '0.1.0', '{}')
            """,
            (map_id, parser_version, family, type, variant, x, y, z, idx),
        )
    conn.commit()


def test_build_graph_writes_nodes_and_edges(seeded) -> None:
    conn, driver = seeded
    parser = "0.0.0"
    map_id = _insert_map(conn, "Mgraph1", parser)
    # Three blocks in a row: straight-curve-straight
    _insert_placement(conn, map_id=map_id, parser_version=parser, idx=0,
                      x=0, y=0, z=0, type="straight")
    _insert_placement(conn, map_id=map_id, parser_version=parser, idx=1,
                      x=1, y=0, z=0, type="curve")
    _insert_placement(conn, map_id=map_id, parser_version=parser, idx=2,
                      x=2, y=0, z=0, type="straight")

    pipeline = ConstraintGraphPipeline(
        mariadb=conn, neo4j_driver=driver, stage_version="0.1.0"
    )
    stats = pipeline.run(snapshot_id=_TEST_SNAPSHOT)
    assert stats.maps_processed == 1

    with driver.session() as s:
        nodes = s.run(
            "MATCH (b:Block) RETURN b.key AS key ORDER BY key"
        ).data()
        node_keys = {r["key"] for r in nodes}
        assert {"tech|curve|", "tech|straight|"}.issubset(node_keys)

        edges = s.run(
            "MATCH (a:Block)-[r:ADJACENT_TO]->(b:Block) "
            "RETURN a.key AS a, b.key AS b, "
            "  r.observed_in_maps_count AS c, r.validity_label AS v "
            "ORDER BY a, b"
        ).data()
        pairs = {(e["a"], e["b"]) for e in edges}
    # Expect (curve, straight) since order is lexicographic.
    assert ("tech|curve|", "tech|straight|") in pairs
    # No benchmark evidence -> validity_label = unknown.
    assert all(e["v"] == "unknown" for e in edges)


def test_build_graph_is_idempotent_via_processed_map(seeded) -> None:
    conn, driver = seeded
    parser = "0.0.0"
    map_id = _insert_map(conn, "Mgraph2", parser)
    _insert_placement(conn, map_id=map_id, parser_version=parser, idx=0, x=0, y=0, z=0)
    _insert_placement(conn, map_id=map_id, parser_version=parser, idx=1, x=1, y=0, z=0, type="curve")

    pipeline = ConstraintGraphPipeline(
        mariadb=conn, neo4j_driver=driver, stage_version="0.1.0"
    )
    first = pipeline.run(snapshot_id=_TEST_SNAPSHOT)
    second = pipeline.run(snapshot_id=_TEST_SNAPSHOT)
    assert first.maps_processed == 1
    assert second.maps_processed == 0
    assert second.maps_skipped_already_processed == 1

    with driver.session() as s:
        count = s.run(
            "MATCH ()-[r:ADJACENT_TO]->() "
            "WHERE r.last_seen_snapshot = $s RETURN r.observed_in_maps_count AS c",
            s=_TEST_SNAPSHOT,
        ).single()
    # Only one map contributed -> count is 1 despite two runs.
    assert count and count["c"] == 1


def test_benchmark_strong_flag_upgrades_to_valid(seeded) -> None:
    conn, driver = seeded
    parser = "0.0.0"
    map_id = _insert_map(conn, "Mgraph3", parser)
    _insert_placement(conn, map_id=map_id, parser_version=parser, idx=0, x=0, y=0, z=0)
    _insert_placement(conn, map_id=map_id, parser_version=parser, idx=1, x=1, y=0, z=0, type="curve")

    pipeline = ConstraintGraphPipeline(
        mariadb=conn,
        neo4j_driver=driver,
        stage_version="0.1.0",
        benchmark_strong_map_ids={map_id},
    )
    pipeline.run(snapshot_id=_TEST_SNAPSHOT)

    with driver.session() as s:
        result = s.run(
            "MATCH ()-[r:ADJACENT_TO]->() "
            "WHERE r.last_seen_snapshot = $s "
            "RETURN r.validity_label AS v, r.benchmark_strong_count AS bc",
            s=_TEST_SNAPSHOT,
        ).single()
    assert result["v"] == "valid"
    assert result["bc"] == 1


def test_broken_fixture_only_is_suspicious(seeded) -> None:
    conn, driver = seeded
    parser = "0.0.0"
    map_id = _insert_map(conn, "Mgraph4", parser)
    _insert_placement(conn, map_id=map_id, parser_version=parser, idx=0, x=0, y=0, z=0, type="bad_a")
    _insert_placement(conn, map_id=map_id, parser_version=parser, idx=1, x=1, y=0, z=0, type="bad_b")

    pipeline = ConstraintGraphPipeline(
        mariadb=conn,
        neo4j_driver=driver,
        stage_version="0.1.0",
        broken_fixture_map_ids={map_id},
    )
    pipeline.run(snapshot_id=_TEST_SNAPSHOT)

    with driver.session() as s:
        result = s.run(
            "MATCH (a:Block {key: 'tech|bad_a|'})-[r:ADJACENT_TO]-(b:Block {key: 'tech|bad_b|'}) "
            "RETURN r.validity_label AS v, r.broken_fixture_count AS c",
        ).single()
    assert result["v"] == "suspicious"
    assert result["c"] == 1


def test_high_count_no_evidence_stays_unknown(seeded) -> None:
    conn, driver = seeded
    parser = "0.0.0"
    # Ten different maps all containing the same (straight, curve) adjacency.
    for i in range(10):
        mid = _insert_map(conn, f"Mgraph5_{i}", parser)
        _insert_placement(conn, map_id=mid, parser_version=parser, idx=0, x=0, y=0, z=0, type="straight")
        _insert_placement(conn, map_id=mid, parser_version=parser, idx=1, x=1, y=0, z=0, type="curve")

    pipeline = ConstraintGraphPipeline(
        mariadb=conn, neo4j_driver=driver, stage_version="0.1.0"
    )
    pipeline.run(snapshot_id=_TEST_SNAPSHOT)

    with driver.session() as s:
        result = s.run(
            "MATCH ()-[r:ADJACENT_TO]->() "
            "WHERE r.last_seen_snapshot = $s "
            "RETURN r.validity_label AS v, r.observed_in_maps_count AS c",
            s=_TEST_SNAPSHOT,
        ).single()
    # High observed count but no evidence -> stays unknown (invariant).
    assert result["c"] == 10
    assert result["v"] == "unknown"
