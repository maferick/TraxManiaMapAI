"""Tests for model_metrics persistence — in-memory fake cursor so no
live DB is needed."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from src.learning import metrics_persistence as mp


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.executed_many: list[tuple[str, list[tuple]]] = []
        self._result_rows: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.executed.append((sql, params))

    def executemany(self, sql: str, batch: list[tuple]) -> None:
        self.executed_many.append((sql, batch))

    def fetchone(self) -> tuple | None:
        return self._result_rows[0] if self._result_rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._result_rows)

    def seed(self, rows: list[tuple]) -> None:
        self._result_rows = list(rows)


class _FakeConn:
    def __init__(self) -> None:
        self.cursor_obj = _FakeCursor()
        self.committed = 0

    def commit(self) -> None:
        self.committed += 1


@contextmanager
def _fake_cursor(conn: _FakeConn) -> Any:
    yield conn.cursor_obj


@pytest.fixture(autouse=True)
def _patch_cursor(monkeypatch) -> None:
    monkeypatch.setattr(mp, "cursor", _fake_cursor)


def _sample_row(*, scheme: str = "time_envelope_v2_weighted") -> tuple:
    """Mirror of the SELECT column order in metrics_persistence."""
    return (
        42,                       # id
        "run-abc",                # run_id
        datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc),
        "f" * 64,                 # model_hash
        scheme,                   # scheme
        1.0,                      # alpha
        876,                      # n_labeled
        0.14,                     # train_rmse
        0.16,                     # test_rmse
        0.34,                     # test_rank_corr
        0.08,                     # heuristic_rank_corr
        0.13,                     # pred_stdev
        0.17,                     # heuristic_stdev
        0.76,                     # pred_stdev_ratio
        0.75,                     # auc_learned
        0.61,                     # auc_heuristic
        0.14,                     # auc_delta
        -0.04,                    # diversity_delta_median
        -0.003,                   # diversity_delta_mean
        0.68,                     # ai_quality_score
        0.82,                     # variety_score
        "2026-04-scale-3k",       # snapshot_filter
        "deadbeef",               # code_version
        "c0ffee",                 # config_hash
    )


class TestNewRunId:
    def test_returns_hex_string(self) -> None:
        rid = mp.new_run_id()
        assert isinstance(rid, str)
        assert len(rid) == 16
        int(rid, 16)  # hex-parseable


class TestRecordMany:
    def test_empty_batch_is_noop(self) -> None:
        conn = _FakeConn()
        assert mp.record_many(conn, []) == 0
        assert not conn.cursor_obj.executed_many
        assert conn.committed == 0

    def test_single_row_inserted(self) -> None:
        conn = _FakeConn()
        row = mp.MetricInsert(
            run_id="r1", model_hash="f" * 64,
            scheme="time_envelope_v2_weighted", alpha=1.0,
            n_labeled=876, code_version="sha", config_hash="cfg",
            test_rank_corr=0.34, auc_delta=0.14,
            ai_quality_score=0.68,
        )
        n = mp.record_many(conn, [row])
        assert n == 1
        assert len(conn.cursor_obj.executed_many) == 1
        sql, batch = conn.cursor_obj.executed_many[0]
        assert "INSERT INTO model_metrics" in sql
        assert "ON DUPLICATE KEY UPDATE" in sql
        assert len(batch) == 1
        # Run_id is first in the payload tuple.
        assert batch[0][0] == "r1"
        assert conn.committed == 1


class TestLatestPerScheme:
    def test_maps_rows_by_scheme(self) -> None:
        conn = _FakeConn()
        conn.cursor_obj.seed([
            _sample_row(scheme="inverse_rank"),
            _sample_row(scheme="time_envelope_v2_weighted"),
        ])
        result = mp.latest_per_scheme(conn)
        assert set(result.keys()) == {
            "inverse_rank", "time_envelope_v2_weighted",
        }
        for row in result.values():
            assert row.run_id == "run-abc"
            # Naive datetimes must be stamped UTC on read.
            assert row.recorded_at.tzinfo is not None

    def test_empty_table_empty_result(self) -> None:
        conn = _FakeConn()
        assert mp.latest_per_scheme(conn) == {}


class TestHistoryForScheme:
    def test_returns_rows_oldest_first_conceptually(self) -> None:
        conn = _FakeConn()
        conn.cursor_obj.seed([
            _sample_row(scheme="time_envelope_v2_weighted"),
            _sample_row(scheme="time_envelope_v2_weighted"),
        ])
        rows = mp.history_for_scheme(conn, "time_envelope_v2_weighted", limit=5)
        assert len(rows) == 2
        for r in rows:
            assert r.scheme == "time_envelope_v2_weighted"
