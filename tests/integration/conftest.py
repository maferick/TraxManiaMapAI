"""Gating: integration tests skip unless the local MariaDB is reachable.

Uses the settings file pointed to by TRAX_CONFIG (default
``config/settings.yaml``). Migrations are assumed already applied.
"""
from __future__ import annotations

import os

import pymysql
import pytest

from src.storage.mariadb import open_connection
from src.utils.config import load_config

_TEST_SNAPSHOT = "integration-test"


@pytest.fixture(scope="session")
def config() -> dict:
    try:
        return load_config()
    except FileNotFoundError:
        pytest.skip("no config/settings.yaml; skipping integration tests")


@pytest.fixture
def db_conn(config: dict):
    try:
        conn = open_connection(config)
    except pymysql.MySQLError as exc:
        pytest.skip(f"MariaDB not reachable: {exc}")
    yield conn
    _cleanup(conn)
    conn.close()


def _cleanup(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM stage_runs WHERE input_ref LIKE %s", (f"%{_TEST_SNAPSHOT}%",))
        cur.execute(
            "DELETE FROM maps WHERE ingestion_snapshot = %s",
            (_TEST_SNAPSHOT,),
        )
        cur.execute(
            "DELETE FROM ingestion_snapshots WHERE snapshot_id = %s",
            (_TEST_SNAPSHOT,),
        )
    conn.commit()


@pytest.fixture
def test_snapshot() -> str:
    return _TEST_SNAPSHOT
