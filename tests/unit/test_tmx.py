from __future__ import annotations

from pathlib import Path

import responses

from src.ingestion.cache import ResponseCache
from src.ingestion.http import HttpClient
from src.ingestion.rate_limit import TokenBucket
from src.ingestion.tmx import TmxClient, TmxMapSummary


_MINIMAL_FIELDS = ("MapId", "Name")


def _make_client(
    tmp_path: Path, *, fields: tuple[str, ...] = _MINIMAL_FIELDS
) -> TmxClient:
    http = HttpClient(
        base_url="https://tmx.test",
        user_agent="test-ua/0.1",
        rate_limiter=TokenBucket(rate_per_second=1000.0),
        cache=ResponseCache(tmp_path / "cache"),
        sleep=lambda _: None,
    )
    return TmxClient(http, summary_fields=fields, page_size=10)


def _map_entry(map_id: int, *, name: str = "map", **extra) -> dict:
    return {"MapId": map_id, "Name": name, **extra}


@responses.activate
def test_iter_paginates_with_after(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/api/maps",
        match=[
            responses.matchers.query_param_matcher(
                {"fields": "MapId,Name", "count": "10"}
            )
        ],
        json={
            "Results": [
                _map_entry(1, name="A"),
                _map_entry(2, name="B"),
            ],
            "More": True,
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://tmx.test/api/maps",
        match=[
            responses.matchers.query_param_matcher(
                {"fields": "MapId,Name", "count": "10", "after": "2"}
            )
        ],
        json={"Results": [_map_entry(3, name="C")], "More": False},
        status=200,
    )
    client = _make_client(tmp_path)
    summaries = list(client.iter_map_summaries())
    assert [s.tmx_id for s in summaries] == ["1", "2", "3"]
    assert isinstance(summaries[0], TmxMapSummary)
    assert summaries[0].title == "A"


@responses.activate
def test_iter_stops_when_more_false(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/api/maps",
        json={"Results": [_map_entry(1)], "More": False},
        status=200,
    )
    client = _make_client(tmp_path)
    out = list(client.iter_map_summaries())
    assert len(out) == 1
    assert len(responses.calls) == 1


@responses.activate
def test_iter_stops_on_empty_results(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/api/maps",
        json={"Results": [], "More": True},  # contradictory but we stop
        status=200,
    )
    client = _make_client(tmp_path)
    assert list(client.iter_map_summaries()) == []


@responses.activate
def test_iter_skips_entries_without_mapid(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/api/maps",
        json={
            "Results": [
                {"Name": "no id"},
                _map_entry(42, name="has id"),
                "not even an object",
                None,
            ],
            "More": False,
        },
        status=200,
    )
    client = _make_client(tmp_path)
    assert [s.tmx_id for s in client.iter_map_summaries()] == ["42"]


@responses.activate
def test_normalize_extracts_authors_and_tags(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/api/maps",
        json={
            "Results": [
                {
                    "MapId": 7,
                    "Name": "Canyon Run",
                    "Authors": [
                        {"User": {"UserId": 1, "Name": "alice"}, "Role": "Mapper"},
                        {"User": {"UserId": 2, "Name": "bob"}, "Role": "Mapper"},
                    ],
                    "Tags": [
                        {"TagId": 2, "Name": "FullSpeed", "Color": ""},
                        {"TagId": 5, "Name": "Tech", "Color": ""},
                    ],
                    "Length": 34000,
                    "AwardCount": 12,
                    "TitlePack": "TMStadium",
                    "Environment": 1,
                }
            ],
            "More": False,
        },
        status=200,
    )
    client = _make_client(tmp_path)
    summaries = list(client.iter_map_summaries())
    assert len(summaries) == 1
    s = summaries[0]
    assert s.author == "alice"
    assert s.style_tags_raw == ["FullSpeed", "Tech"]
    assert s.length_estimate_ms == 34000
    assert s.award_count == 12
    assert s.environment == "TMStadium"


@responses.activate
def test_random_mode_makes_one_call_per_map(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/api/maps",
        match=[
            responses.matchers.query_param_matcher(
                {"fields": "MapId,Name", "random": "1", "count": "1"}
            )
        ],
        json={"Results": [_map_entry(100, name="Random A")]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://tmx.test/api/maps",
        json={"Results": [_map_entry(101, name="Random B")]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://tmx.test/api/maps",
        json={"Results": [_map_entry(102, name="Random C")]},
        status=200,
    )
    client = _make_client(tmp_path)
    randoms = list(client.iter_random_summaries(count=3))
    assert [s.tmx_id for s in randoms] == ["100", "101", "102"]
    assert len(responses.calls) == 3


def test_random_mode_count_zero_yields_nothing(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    assert list(client.iter_random_summaries(count=0)) == []


@responses.activate
def test_fetch_map_detail_unwraps_results(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/api/maps/42",
        json={"Results": [{"MapId": 42, "Name": "X"}], "More": False},
        status=200,
    )
    client = _make_client(tmp_path)
    detail = client.fetch_map_detail("42")
    assert detail["MapId"] == 42


@responses.activate
def test_download_map_artifact_hits_mapgbx(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/mapgbx/7",
        body=b"gbx-bytes",
        status=200,
    )
    client = _make_client(tmp_path)
    r = client.download_map_artifact("7")
    assert r.content == b"gbx-bytes"
    # Subsequent call should not hit the cache (use_cache=False).
    responses.add(
        responses.GET,
        "https://tmx.test/mapgbx/7",
        body=b"gbx-bytes",
        status=200,
    )
    client.download_map_artifact("7")
    assert len(responses.calls) == 2


@responses.activate
def test_fetch_tags_returns_list(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/api/meta/tags",
        json=[
            {"ID": 1, "Name": "Race", "Color": ""},
            {"ID": 2, "Name": "FullSpeed", "Color": ""},
        ],
        status=200,
    )
    client = _make_client(tmp_path)
    tags = client.fetch_tags()
    assert [t["Name"] for t in tags] == ["Race", "FullSpeed"]


def test_endpoint_overrides_merge_with_defaults(tmp_path: Path) -> None:
    http = HttpClient(
        base_url="https://tmx.test",
        user_agent="ua",
        rate_limiter=TokenBucket(rate_per_second=1000.0),
    )
    client = TmxClient(http, endpoints={"list_maps": "/v3/maps"})
    assert client._endpoints["list_maps"] == "/v3/maps"
    assert client._endpoints["map_download"] == "/mapgbx/{id}"
    assert client._endpoints["meta_tags"] == "/api/meta/tags"
