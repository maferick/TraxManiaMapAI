"""ManiaExchange / TMX adapter (v2 API).

Wire format (as of 2026-04, confirmed against trackmania.exchange):

- ``GET /api/maps?fields=<csv>&count=<n>[&after=<last_MapId>]`` returns
  ``{"Results": [...], "More": <bool>}``. The ``fields`` parameter is
  mandatory; the adapter holds a default list that covers everything
  :class:`TmxMapSummary` surfaces.
- ``GET /api/maps?fields=...&random=1&count=1`` returns one random
  map. Used for the ``download-sample-random`` CLI path.
- ``GET /mapgbx/{MapId}`` returns the ``.Map.Gbx`` bytes
  (``application/x-gbx``). The legacy ``/maps/download/{id}`` URL
  303-redirects here; ``requests`` follows the redirect.

Conventions enforced by the site (ManiaExchange "Conventions" page,
last updated 2024-12-08):

- ``User-Agent`` header is required (set at :class:`HttpClient`
  construction)
- Only ``application/json`` is supported for formatted responses
- Only request the fields you need
- Cache aggressively
- Timestamps are UTC

All of these are handled either here or in the ``HttpClient``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

from .http import HttpClient, HttpError, HttpResponse

_LOG = logging.getLogger(__name__)


_DEFAULT_ENDPOINTS: dict[str, str] = {
    "list_maps": "/api/maps",
    "map_detail": "/api/maps/{id}",
    "map_download": "/mapgbx/{id}",
    "meta_tags": "/api/meta/tags",
    "meta_vehicles": "/api/meta/vehicles",
    # Replay endpoints. Marked "deprecated" in the API docs (a v2
    # search-replays method is planned) but are the only public way
    # to enumerate + download replays today. The list endpoint
    # 303-redirects to /legacyreplays/{mapId}; requests follows it.
    "list_replays": "/api/replays/get_replays/{map_id}",
    "replay_detail": "/api/replays/get_replay_info/{replay_id}",
    "replay_download": "/recordgbx/{replay_id}",
}

# Minimal summary field set. Adding a field here costs bytes per request
# but unlocks new TmxMapSummary columns. Keep it tight.
_DEFAULT_SUMMARY_FIELDS: tuple[str, ...] = (
    "MapId",
    "Name",
    "GbxMapName",
    "MapUid",
    "Authors",
    "Tags",
    "AwardCount",
    "Length",
    "Difficulty",
    "UpdatedAt",
    "UploadedAt",
    "TitlePack",
    "Environment",
)


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


@dataclass(frozen=True)
class TmxReplaySummary:
    """One leaderboard entry from ``/api/replays/get_replays/{mapId}``."""
    replay_id: str                 # from ReplayID
    map_source_id: str             # from TrackID (the TMX map id as string)
    player_display_name: str | None
    finish_time_ms: int | None
    position: int | None
    beaten: int | None
    stunt_score: int | None
    respawns: int | None
    player_model: str | None
    replay_uploaded_at: str | None  # ReplayAt (ISO-8601 string)
    exe_build: str | None
    raw: dict[str, Any]


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if s else None


def _as_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _extract_author(raw: Mapping[str, Any]) -> str | None:
    authors = raw.get("Authors")
    if not isinstance(authors, list) or not authors:
        return None
    first = authors[0]
    if not isinstance(first, Mapping):
        return None
    user = first.get("User")
    if isinstance(user, Mapping):
        return _as_str(user.get("Name"))
    return _as_str(first.get("Name"))


def _extract_tags(raw: Mapping[str, Any]) -> list[str]:
    tags = raw.get("Tags")
    if not isinstance(tags, list):
        return []
    out: list[str] = []
    for t in tags:
        if isinstance(t, Mapping):
            name = _as_str(t.get("Name"))
            if name:
                out.append(name)
        elif t is not None:
            out.append(str(t))
    return out


def _normalize_replay(raw: Mapping[str, Any]) -> TmxReplaySummary | None:
    replay_id = _as_str(raw.get("ReplayID"))
    track_id = _as_str(raw.get("TrackID"))
    if not replay_id or not track_id:
        return None
    return TmxReplaySummary(
        replay_id=replay_id,
        map_source_id=track_id,
        player_display_name=_as_str(raw.get("Username")),
        finish_time_ms=_as_int(raw.get("ReplayTime")),
        position=_as_int(raw.get("Position")),
        beaten=_as_int(raw.get("Beaten")),
        stunt_score=_as_int(raw.get("StuntScore")),
        respawns=_as_int(raw.get("Respawns")),
        player_model=_as_str(raw.get("PlayerModel")),
        replay_uploaded_at=_as_str(raw.get("ReplayAt")),
        exe_build=_as_str(raw.get("ExeBuild")),
        raw=dict(raw),
    )


def _normalize_summary(raw: Mapping[str, Any]) -> TmxMapSummary | None:
    tmx_id = _as_str(raw.get("MapId"))
    if not tmx_id:
        return None
    return TmxMapSummary(
        tmx_id=tmx_id,
        title=_as_str(raw.get("Name") or raw.get("GbxMapName")),
        author=_extract_author(raw),
        # TitlePack is more human-readable than the numeric Environment code.
        environment=_as_str(raw.get("TitlePack") or raw.get("Environment")),
        style_tags_raw=_extract_tags(raw),
        length_estimate_ms=_as_int(raw.get("Length")),
        award_count=_as_int(raw.get("AwardCount")),
        # The v2 API doesn't expose a direct rating or popularity number
        # on the summary; these stay None and can be enriched later.
        average_rating=None,
        popularity_metric=None,
        # has_items requires an anchored-objects query; mark unknown=False
        # on the summary. The parser result carries the ground truth.
        has_items=False,
        # TM2020 maps are block-mode by default; no flag on the v2 shape.
        is_block_mode=True,
        raw=dict(raw),
    )


class TmxClient:
    def __init__(
        self,
        http: HttpClient,
        *,
        endpoints: dict[str, str] | None = None,
        summary_fields: Sequence[str] | None = None,
        page_size: int = 100,
    ) -> None:
        merged = dict(_DEFAULT_ENDPOINTS)
        if endpoints:
            merged.update(endpoints)
        self._http = http
        self._endpoints = merged
        self._summary_fields = tuple(summary_fields or _DEFAULT_SUMMARY_FIELDS)
        self._page_size = int(page_size)

    def _summary_fields_param(self) -> str:
        return ",".join(self._summary_fields)

    def _get_json(
        self,
        path: str,
        params: Mapping[str, object],
        *,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        response = self._http.get(path, params=params, use_cache=use_cache)
        if response.status_code != 200:
            raise HttpError(
                f"{path} returned status {response.status_code}",
                status_code=response.status_code,
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise HttpError(f"{path} payload is not a JSON object")
        return payload

    def iter_map_summaries(
        self, *, after_map_id: int | None = None
    ) -> Iterator[TmxMapSummary]:
        """Yield summaries, paging by ``after={last MapId}`` until ``More`` is False."""
        last_seen = after_map_id
        while True:
            params: dict[str, object] = {
                "fields": self._summary_fields_param(),
                "count": self._page_size,
            }
            if last_seen is not None:
                params["after"] = last_seen
            payload = self._get_json(self._endpoints["list_maps"], params)
            results = payload.get("Results")
            if not isinstance(results, list) or not results:
                return
            emitted_any = False
            for raw in results:
                if not isinstance(raw, Mapping):
                    continue
                summary = _normalize_summary(raw)
                if summary is None:
                    _LOG.warning("skipping TMX entry without MapId: %r", raw)
                    continue
                emitted_any = True
                try:
                    last_seen = int(summary.tmx_id)
                except ValueError:
                    pass
                yield summary
            if not payload.get("More") or not emitted_any:
                return

    def iter_random_summaries(self, *, count: int) -> Iterator[TmxMapSummary]:
        """Yield ``count`` independent random summaries.

        The API's ``random=1`` mode only returns one map per call, so this
        hits the endpoint ``count`` times. Each call is rate-limited by
        the underlying ``HttpClient``.
        """
        if count <= 0:
            return
        emitted = 0
        while emitted < count:
            # Random mode must never be cache-backed — every call is
            # semantically a different map even though the URL is the same.
            payload = self._get_json(
                self._endpoints["list_maps"],
                {
                    "fields": self._summary_fields_param(),
                    "random": 1,
                    "count": 1,
                },
                use_cache=False,
            )
            results = payload.get("Results") or []
            if not results:
                return
            raw = results[0]
            if not isinstance(raw, Mapping):
                continue
            summary = _normalize_summary(raw)
            if summary is None:
                continue
            yield summary
            emitted += 1

    def fetch_map_detail(self, tmx_id: str) -> dict[str, Any]:
        path = self._endpoints["map_detail"].format(id=tmx_id)
        payload = self._get_json(path, {"fields": self._summary_fields_param()})
        # The single-id endpoint may wrap in Results[] or return a bare object.
        results = payload.get("Results")
        if isinstance(results, list) and results:
            first = results[0]
            if isinstance(first, Mapping):
                return dict(first)
        return payload

    def download_map_artifact(self, tmx_id: str) -> HttpResponse:
        return self._http.get(
            self._endpoints["map_download"].format(id=tmx_id),
            use_cache=False,
        )

    def fetch_tags(self) -> list[dict[str, Any]]:
        """Fetch the site's tag taxonomy.

        Returns the raw ``[{ID, Name, Color}, ...]`` list. This endpoint
        has no pagination, no ``fields`` parameter, and returns an array
        (not the ``{Results, More}`` envelope).
        """
        response = self._http.get(self._endpoints["meta_tags"])
        if response.status_code != 200:
            raise HttpError(
                f"meta_tags returned status {response.status_code}",
                status_code=response.status_code,
            )
        payload = response.json()
        if not isinstance(payload, list):
            raise HttpError(
                f"meta_tags payload is not a JSON array (got {type(payload).__name__})"
            )
        return [dict(t) for t in payload if isinstance(t, Mapping)]

    def fetch_vehicles(self) -> list[str]:
        """Fetch the distinct-vehicle list (``/api/meta/vehicles``).

        Returns a plain ``list[str]`` of vehicle model names. Order is
        "official vehicles first, then custom in descending occurrence
        count" per the API docs — so the early entries are the
        environment-specific defaults (``CanyonCar``, ``StadiumCar``,
        ``ValleyCar``, ...).
        """
        response = self._http.get(self._endpoints["meta_vehicles"])
        if response.status_code != 200:
            raise HttpError(
                f"meta_vehicles returned status {response.status_code}",
                status_code=response.status_code,
            )
        payload = response.json()
        if not isinstance(payload, list):
            raise HttpError(
                f"meta_vehicles payload is not a JSON array (got {type(payload).__name__})"
            )
        return [str(v) for v in payload if v is not None]

    # ------------------------------------------------------------------
    # Replay endpoints

    def iter_replays_for_map(
        self, map_source_id: str, *, amount: int | None = None
    ) -> Iterator[TmxReplaySummary]:
        """Yield up to 25 replays uploaded for a given map.

        The deprecated endpoint caps at 25 regardless of ``amount``; we
        still pass it through for when the v2 method lands. The 303
        redirect to ``/legacyreplays/{mapId}`` is handled transparently
        by ``requests``.
        """
        path = self._endpoints["list_replays"].format(map_id=map_source_id)
        params: dict[str, object] = {}
        if amount is not None:
            params["amount"] = int(amount)
        payload = self._get_json(path, params)
        results = payload.get("Results")
        if not isinstance(results, list):
            return
        for raw in results:
            if not isinstance(raw, Mapping):
                continue
            summary = _normalize_replay(raw)
            if summary is None:
                continue
            yield summary

    def fetch_replay_detail(self, replay_id: str) -> dict[str, Any]:
        path = self._endpoints["replay_detail"].format(replay_id=replay_id)
        payload = self._get_json(path, {})
        results = payload.get("Results")
        if isinstance(results, list) and results:
            first = results[0]
            if isinstance(first, Mapping):
                return dict(first)
        return payload

    def download_replay_artifact(self, replay_id: str) -> HttpResponse:
        return self._http.get(
            self._endpoints["replay_download"].format(replay_id=replay_id),
            use_cache=False,
        )
