from __future__ import annotations

from pathlib import Path

import responses

from src.ingestion.cache import ResponseCache
from src.ingestion.http import HttpClient
from src.ingestion.rate_limit import TokenBucket
from src.ingestion.tmx import TmxClient, TmxMapSummary


def _make_client(tmp_path: Path) -> TmxClient:
    http = HttpClient(
        base_url="https://tmx.test",
        user_agent="test-ua/0.1",
        rate_limiter=TokenBucket(rate_per_second=1000.0),
        cache=ResponseCache(tmp_path / "cache"),
        sleep=lambda _: None,
    )
    return TmxClient(http, page_size=10)


@responses.activate
def test_iter_paginates_with_cursor(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/maps",
        match=[responses.matchers.query_param_matcher({"limit": "10"})],
        json={
            "items": [
                {"id": "1", "title": "A", "author": "alice", "tags": ["tech"]},
                {"id": "2", "title": "B", "author": "bob"},
            ],
            "cursor": "page2",
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://tmx.test/maps",
        match=[responses.matchers.query_param_matcher({"limit": "10", "cursor": "page2"})],
        json={"items": [{"id": "3", "title": "C"}], "cursor": None},
        status=200,
    )
    client = _make_client(tmp_path)
    summaries = list(client.iter_map_summaries())
    assert [s.tmx_id for s in summaries] == ["1", "2", "3"]
    assert summaries[0].style_tags_raw == ["tech"]
    assert isinstance(summaries[0], TmxMapSummary)


@responses.activate
def test_iter_skips_entries_without_id(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/maps",
        json={
            "items": [
                {"title": "no id"},
                {"id": "42", "title": "has id"},
                "not even an object",
                None,
            ],
            "cursor": None,
        },
        status=200,
    )
    client = _make_client(tmp_path)
    assert [s.tmx_id for s in client.iter_map_summaries()] == ["42"]


@responses.activate
def test_fetch_map_detail(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/maps/42",
        json={"id": "42", "title": "X"},
        status=200,
    )
    client = _make_client(tmp_path)
    detail = client.fetch_map_detail("42")
    assert detail["id"] == "42"


@responses.activate
def test_download_map_artifact_bypasses_cache(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/maps/7/download",
        body=b"gbx-bytes",
        status=200,
    )
    client = _make_client(tmp_path)
    r = client.download_map_artifact("7")
    assert r.content == b"gbx-bytes"
    # Second call should not be cached (use_cache=False in the adapter),
    # so the underlying mock should be hit again.
    responses.add(
        responses.GET,
        "https://tmx.test/maps/7/download",
        body=b"gbx-bytes",
        status=200,
    )
    client.download_map_artifact("7")
    assert len(responses.calls) == 2


def test_endpoint_overrides_merge_with_defaults(tmp_path: Path) -> None:
    http = HttpClient(
        base_url="https://tmx.test",
        user_agent="ua",
        rate_limiter=TokenBucket(rate_per_second=1000.0),
    )
    client = TmxClient(http, endpoints={"list_maps": "/v2/maps"})
    # Override took effect; other defaults still present.
    assert client._endpoints["list_maps"] == "/v2/maps"
    assert client._endpoints["map_detail"] == "/maps/{id}"
