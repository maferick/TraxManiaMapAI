"""Rate-limited, cached HTTP client for TMX ingestion.

Retries: exponential-backoff on 5xx, 429, and network errors.
Caching: 2xx responses are keyed by method+URL+params hash on disk.
Non-2xx responses are never cached.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import requests

from .cache import ResponseCache
from .rate_limit import TokenBucket

_LOG = logging.getLogger(__name__)


class HttpError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    content: bytes
    headers: Mapping[str, str]
    from_cache: bool

    def json(self) -> Any:
        if not self.content:
            raise HttpError("empty body, cannot decode JSON")
        return json.loads(self.content.decode("utf-8"))


class HttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        user_agent: str,
        rate_limiter: TokenBucket,
        cache: ResponseCache | None = None,
        session: requests.Session | None = None,
        backoff_seconds: Sequence[float] = (2.0, 4.0, 8.0, 16.0),
        timeout_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not user_agent:
            raise ValueError("user_agent is required — TMX identifies clients by UA")
        self._base_url = base_url.rstrip("/")
        self._user_agent = user_agent
        self._rate_limiter = rate_limiter
        self._cache = cache
        self._session = session or requests.Session()
        self._backoff = tuple(backoff_seconds)
        self._timeout = timeout_seconds
        self._sleep = sleep

    def get(
        self,
        path: str,
        params: Mapping[str, object] | None = None,
        *,
        use_cache: bool = True,
    ) -> HttpResponse:
        url = self._build_url(path)
        cache_key = ResponseCache.key_for("GET", url, params) if self._cache else ""

        if use_cache and self._cache is not None:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return HttpResponse(
                    status_code=200,
                    content=cached,
                    headers={},
                    from_cache=True,
                )

        response = self._request_with_retries("GET", url, params=params)
        if use_cache and self._cache is not None and 200 <= response.status_code < 300:
            self._cache.put(cache_key, response.content)
        return response

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        leading = "" if path.startswith("/") else "/"
        return f"{self._base_url}{leading}{path}"

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self._user_agent, "Accept": "application/json"}

    def _request_with_retries(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, object] | None,
    ) -> HttpResponse:
        attempts = len(self._backoff) + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            self._rate_limiter.acquire()
            try:
                raw = self._session.request(
                    method,
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                _LOG.warning("network error on attempt %d for %s: %s", attempt + 1, url, exc)
                if attempt < attempts - 1:
                    self._sleep(self._backoff[attempt])
                    continue
                raise HttpError(f"network error after {attempts} attempts: {exc}") from exc

            status = raw.status_code
            if 200 <= status < 300 or (400 <= status < 500 and status != 429):
                return HttpResponse(
                    status_code=status,
                    content=raw.content,
                    headers=dict(raw.headers),
                    from_cache=False,
                )

            _LOG.warning(
                "retryable HTTP %d on attempt %d for %s", status, attempt + 1, url
            )
            if attempt < attempts - 1:
                self._sleep(self._backoff[attempt])
                continue
            return HttpResponse(
                status_code=status,
                content=raw.content,
                headers=dict(raw.headers),
                from_cache=False,
            )

        raise HttpError(
            f"unreachable retry loop exit for {url}: {last_exc}",
        )
