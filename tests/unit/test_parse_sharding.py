"""Sharded parse selection — disjoint partitions, no claim protocol."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.parsers import pipeline as pl


def _capture_sql(monkeypatch):
    """Return a list that receives (sql, params) from the stub cursor."""
    calls: list[tuple[str, tuple]] = []
    cur = MagicMock()

    def execute_impl(sql, params=()):
        calls.append((sql, params))

    cur.execute.side_effect = execute_impl
    cur.fetchall.return_value = []
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    ctx.__exit__.return_value = False
    monkeypatch.setattr(pl, "cursor", lambda conn: ctx)
    return calls


class TestShardedFetch:
    def test_no_shard_clause_when_single_worker(self, monkeypatch):
        calls = _capture_sql(monkeypatch)
        pl._fetch_unparsed(
            MagicMock(), snapshot_id=None, limit=None, retry_transient=False
        )
        sql, params = calls[0]
        assert "%%" not in sql and " % " not in sql
        assert params == ()

    def test_shard_clause_and_params(self, monkeypatch):
        calls = _capture_sql(monkeypatch)
        pl._fetch_unparsed(
            MagicMock(), snapshot_id="snap", limit=None,
            retry_transient=False, shard_index=2, shard_count=5,
        )
        sql, params = calls[0]
        assert "id %% %s = %s" in sql
        assert params == ("snap", 5, 2)

    def test_shards_partition_ids_disjointly(self):
        # The contract the SQL encodes: every id lands in exactly one
        # shard, so concurrent workers never collide.
        shard_count = 5
        ids = range(1, 1001)
        seen: dict[int, int] = {}
        for shard in range(shard_count):
            for i in ids:
                if i % shard_count == shard:
                    assert i not in seen
                    seen[i] = shard
        assert len(seen) == len(list(ids))
