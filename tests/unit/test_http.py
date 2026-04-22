from __future__ import annotations

import random
from pathlib import Path

import pytest
import requests
import responses

from src.ingestion.cache import ResponseCache
from src.ingestion.http import HttpClient, HttpError, _parse_retry_after
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


def test_parse_retry_after_seconds() -> None:
    assert _parse_retry_after("5") == 5.0
    assert _parse_retry_after("0") == 0.0
    assert _parse_retry_after("  12  ") == 12.0


def test_parse_retry_after_http_date() -> None:
    # "Sun, 06 Nov 1994 08:49:37 GMT" — 30 seconds after our fake now.
    fake_now = lambda: 784111747.0
    got = _parse_retry_after("Sun, 06 Nov 1994 08:49:37 GMT", now=fake_now)
    assert got is not None and 29.0 <= got <= 31.0


def test_parse_retry_after_unparseable_returns_none() -> None:
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("not-a-date") is None


@responses.activate
def test_retry_after_header_is_respected_on_429() -> None:
    responses.add(
        responses.GET,
        "https://tmx.test/ratelimited",
        status=429,
        headers={"Retry-After": "7"},
    )
    responses.add(responses.GET, "https://tmx.test/ratelimited", status=200, json={})
    sleeps: list[float] = []
    client = HttpClient(
        base_url="https://tmx.test",
        user_agent="ua",
        rate_limiter=TokenBucket(rate_per_second=1000.0),
        backoff_seconds=(0.5,),  # scheduled < Retry-After; header should win
        sleep=sleeps.append,
        rng=random.Random(0),
        jitter_range=(1.0, 1.0),  # disable jitter for determinism
    )
    r = client.get("/ratelimited")
    assert r.status_code == 200
    assert sleeps == [7.0]


@responses.activate
def test_retry_after_is_capped_by_deadline() -> None:
    # Server keeps asking us to come back in 120s; deadline is 10s.
    responses.add(
        responses.GET,
        "https://tmx.test/stall",
        status=503,
        headers={"Retry-After": "120"},
    )
    sleeps: list[float] = []
    client = HttpClient(
        base_url="https://tmx.test",
        user_agent="ua",
        rate_limiter=TokenBucket(rate_per_second=1000.0),
        backoff_seconds=(1.0, 1.0, 1.0),
        sleep=sleeps.append,
        rng=random.Random(0),
        jitter_range=(1.0, 1.0),
        max_total_retry_seconds=10.0,
    )
    r = client.get("/stall")
    # Deadline trips before we ever sleep for 120s; no retries occurred.
    assert r.status_code == 503
    assert sleeps == []
    assert len(responses.calls) == 1


@responses.activate
def test_network_error_retry_deadline_raises() -> None:
    for _ in range(3):
        responses.add(
            responses.GET,
            "https://tmx.test/dead",
            body=requests.exceptions.ConnectionError("dead"),
        )
    client = HttpClient(
        base_url="https://tmx.test",
        user_agent="ua",
        rate_limiter=TokenBucket(rate_per_second=1000.0),
        backoff_seconds=(5.0, 5.0),
        sleep=lambda _: None,
        rng=random.Random(0),
        jitter_range=(1.0, 1.0),
        max_total_retry_seconds=3.0,  # first wait (5s) already over the cap
    )
    with pytest.raises(HttpError, match="deadline"):
        client.get("/dead")
    assert len(responses.calls) == 1


@responses.activate
def test_jitter_varies_sleep_amount() -> None:
    responses.add(responses.GET, "https://tmx.test/j", status=503)
    responses.add(responses.GET, "https://tmx.test/j", status=200, json={})
    sleeps: list[float] = []
    # Seeded RNG with a wide jitter range — exact value is deterministic but
    # definitely != the nominal backoff.
    client = HttpClient(
        base_url="https://tmx.test",
        user_agent="ua",
        rate_limiter=TokenBucket(rate_per_second=1000.0),
        backoff_seconds=(10.0,),
        sleep=sleeps.append,
        rng=random.Random(42),
        jitter_range=(0.75, 1.25),
    )
    client.get("/j")
    assert len(sleeps) == 1
    # Jittered value must land inside the declared window and be != nominal.
    assert 7.5 <= sleeps[0] <= 12.5
    assert sleeps[0] != 10.0


@responses.activate
def test_jitter_range_must_bracket_one() -> None:
    with pytest.raises(ValueError):
        HttpClient(
            base_url="https://tmx.test",
            user_agent="ua",
            rate_limiter=TokenBucket(rate_per_second=1.0),
            jitter_range=(1.1, 1.5),
        )


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
