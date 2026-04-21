"""TMX adapter.

Wraps the generic :class:`HttpClient` with TMX-specific endpoint paths
and surfaces normalized summaries to the ingestion orchestrator.

The concrete TMX request/response shapes evolve outside this repo.
This module defines the **Python-side** contract — field names the
orchestrator depends on — and validates each response against it,
skipping malformed entries with a warning rather than crashing the
batch.

Endpoint paths are config-driven (``ingestion.tmx.endpoints``) so that
an upstream schema change is a YAML edit, not a code edit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from .http import HttpClient, HttpError, HttpResponse

_LOG = logging.getLogger(__name__)

_DEFAULT_ENDPOINTS: dict[str, str] = {
    "list_maps": "/maps",
    "map_detail": "/maps/{id}",
    "map_download": "/maps/{id}/download",
}


@dataclass(frozen=True)
class TmxMapSummary:
    tmx_id: str
    title: str | None
    author: str | None
    environment: str | None
    style_tags_raw: list[str]
    length_estimate_ms: int | None
    award_count: int | None
    average_rating: float | None
    popularity_metric: int | None
    has_items: bool
    is_block_mode: bool
    raw: dict[str, Any]


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return bool(value)


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v is not None]


def _normalize_summary(raw: Mapping[str, Any]) -> TmxMapSummary | None:
    tmx_id = _as_str(raw.get("id") or raw.get("tmx_id"))
    if not tmx_id:
        return None
    return TmxMapSummary(
        tmx_id=tmx_id,
        title=_as_str(raw.get("title") or raw.get("name")),
        author=_as_str(raw.get("author")),
        environment=_as_str(raw.get("environment")),
        style_tags_raw=_as_str_list(raw.get("tags")),
        length_estimate_ms=_as_int(raw.get("length_ms") or raw.get("length")),
        award_count=_as_int(raw.get("award_count") or raw.get("awards")),
        average_rating=_as_float(raw.get("average_rating") or raw.get("rating")),
        popularity_metric=_as_int(raw.get("popularity") or raw.get("downloads")),
        has_items=_as_bool(raw.get("has_items"), default=False),
        is_block_mode=_as_bool(raw.get("is_block_mode"), default=True),
        raw=dict(raw),
    )


class TmxClient:
    def __init__(
        self,
        http: HttpClient,
        *,
        endpoints: dict[str, str] | None = None,
        page_size: int = 100,
    ) -> None:
        merged = dict(_DEFAULT_ENDPOINTS)
        if endpoints:
            merged.update(endpoints)
        self._http = http
        self._endpoints = merged
        self._page_size = page_size

    def iter_map_summaries(self, *, start_cursor: str | None = None) -> Iterator[TmxMapSummary]:
        cursor = start_cursor
        seen_pages = 0
        while True:
            params: dict[str, object] = {"limit": self._page_size}
            if cursor is not None:
                params["cursor"] = cursor
            response = self._http.get(self._endpoints["list_maps"], params=params)
            if response.status_code != 200:
                raise HttpError(
                    f"list_maps returned status {response.status_code}",
                    status_code=response.status_code,
                )
            payload = response.json()
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                _LOG.warning("list_maps payload has no 'items' array, stopping")
                return
            for raw in items:
                if not isinstance(raw, Mapping):
                    continue
                summary = _normalize_summary(raw)
                if summary is None:
                    _LOG.warning("skipping TMX entry without id: %r", raw)
                    continue
                yield summary
            seen_pages += 1
            cursor = payload.get("cursor") if isinstance(payload, dict) else None
            if not cursor:
                return

    def fetch_map_detail(self, tmx_id: str) -> dict[str, Any]:
        response = self._http.get(self._endpoints["map_detail"].format(id=tmx_id))
        if response.status_code != 200:
            raise HttpError(
                f"map_detail {tmx_id} returned {response.status_code}",
                status_code=response.status_code,
            )
        detail = response.json()
        if not isinstance(detail, dict):
            raise HttpError(f"map_detail {tmx_id} payload is not an object")
        return detail

    def download_map_artifact(self, tmx_id: str) -> HttpResponse:
        return self._http.get(
            self._endpoints["map_download"].format(id=tmx_id),
            use_cache=False,
        )
