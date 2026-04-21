from __future__ import annotations

from pathlib import Path

import pytest
import requests
import responses

from src.ingestion.cache import ResponseCache
from src.ingestion.http import HttpClient, HttpError
from src.ingestion.rate_limit import TokenBucket


def _client(tmp_path: Path, *, cache: bool = True) -> HttpClient:
    return HttpClient(
        base_url="https://tmx.test",
        user_agent="test-ua/0.1",
        rate_limiter=TokenBucket(rate_per_second=1000.0),
        cache=ResponseCache(tmp_path / "cache") if cache else None,
        backoff_seconds=(),  # no retries; override per-test
        timeout_seconds=5.0,
        sleep=lambda _: None,
    )


@responses.activate
def test_rejects_missing_user_agent() -> None:
    with pytest.raises(ValueError):
        HttpClient(
            base_url="https://tmx.test",
            user_agent="",
            rate_limiter=TokenBucket(rate_per_second=1.0),
        )


@responses.activate
def test_get_success_then_cached(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/maps",
        json={"items": [], "cursor": None},
        status=200,
    )
    client = _client(tmp_path)
    r1 = client.get("/maps", params={"limit": 10})
    assert r1.status_code == 200
    assert r1.from_cache is False
    r2 = client.get("/maps", params={"limit": 10})
    assert r2.from_cache is True
    assert r1.content == r2.content
    assert len(responses.calls) == 1


@responses.activate
def test_four_xx_is_not_retried(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/missing",
        json={"error": "not found"},
        status=404,
    )
    client = HttpClient(
        base_url="https://tmx.test",
        user_agent="ua",
        rate_limiter=TokenBucket(rate_per_second=1000.0),
        backoff_seconds=(0.01, 0.01, 0.01),
        sleep=lambda _: None,
    )
    r = client.get("/missing")
    assert r.status_code == 404
    assert len(responses.calls) == 1


@responses.activate
def test_five_xx_is_retried_until_success(tmp_path: Path) -> None:
    responses.add(responses.GET, "https://tmx.test/flaky", status=503)
    responses.add(responses.GET, "https://tmx.test/flaky", status=503)
    responses.add(
        responses.GET, "https://tmx.test/flaky", json={"ok": True}, status=200
    )
    client = HttpClient(
        base_url="https://tmx.test",
        user_agent="ua",
        rate_limiter=TokenBucket(rate_per_second=1000.0),
        backoff_seconds=(0.0, 0.0, 0.0),
        sleep=lambda _: None,
    )
    r = client.get("/flaky")
    assert r.status_code == 200
    assert len(responses.calls) == 3


@responses.activate
def test_rate_limited_429_is_retried(tmp_path: Path) -> None:
    responses.add(responses.GET, "https://tmx.test/fast", status=429)
    responses.add(responses.GET, "https://tmx.test/fast", status=200, json={})
    client = HttpClient(
        base_url="https://tmx.test",
        user_agent="ua",
        rate_limiter=TokenBucket(rate_per_second=1000.0),
        backoff_seconds=(0.0, 0.0),
        sleep=lambda _: None,
    )
    r = client.get("/fast")
    assert r.status_code == 200
    assert len(responses.calls) == 2


@responses.activate
def test_network_error_is_retried_then_raises(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/dead",
        body=requests.exceptions.ConnectionError("dead"),
    )
    responses.add(
        responses.GET,
        "https://tmx.test/dead",
        body=requests.exceptions.ConnectionError("dead"),
    )
    client = HttpClient(
        base_url="https://tmx.test",
        user_agent="ua",
        rate_limiter=TokenBucket(rate_per_second=1000.0),
        backoff_seconds=(0.0,),
        sleep=lambda _: None,
    )
    with pytest.raises(HttpError, match="network"):
        client.get("/dead")


@responses.activate
def test_error_response_is_not_cached(tmp_path: Path) -> None:
    responses.add(responses.GET, "https://tmx.test/x", status=404, json={})
    client = _client(tmp_path)
    client.get("/x")
    # A second call should hit the network again because 404 wasn't cached.
    client.get("/x")
    assert len(responses.calls) == 2


@responses.activate
def test_absolute_url_bypasses_base(tmp_path: Path) -> None:
    responses.add(responses.GET, "https://other.test/foo", json={"ok": 1}, status=200)
    client = _client(tmp_path, cache=False)
    r = client.get("https://other.test/foo")
    assert r.status_code == 200


@responses.activate
def test_user_agent_header_is_sent(tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    def _capture(request: requests.PreparedRequest) -> tuple[int, dict, str]:
        captured.update(dict(request.headers))
        return (200, {}, "{}")

    responses.add_callback(responses.GET, "https://tmx.test/who", callback=_capture)
    client = _client(tmp_path, cache=False)
    client.get("/who")
    assert captured.get("User-Agent") == "test-ua/0.1"
