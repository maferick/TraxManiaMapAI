"""Map-related entities: canonical map metadata + block placements.

Schema mirrors ``migrations/mariadb/003_maps.sql`` and
``migrations/mariadb/004_block_placements.sql``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.parsers.errors import ParseErrorCode, ParseStatus


@dataclass(frozen=True)
class Map:
    id: int | None
    source_system: str
    source_map_id: str
    ingestion_snapshot: str
    parser_version: str
    created_by_version: str

    title: str | None = None
    author: str | None = None
    environment: str | None = None
    style_tags_raw: list[str] | None = None
    length_estimate_ms: int | None = None
    award_count: int | None = None
    average_rating: Decimal | None = None
    popularity_metric: int | None = None
    has_items: bool = False
    is_block_mode: bool = True

    # Scenery metadata (migration 011). Populated by the map-parse
    # stage; see src/parsers/pipeline.py::_scenery_params. Kept as
    # aggregate counts on the map row — per-item rows are out of
    # scope until a use case demands them.
    mood: str | None = None
    decoration_id: str | None = None
    day_time_seconds: int | None = None
    dynamic_daylight: bool | None = None
    scenery_item_count: int | None = None
    signpost_count: int | None = None
    scenery_standard_item_count: int | None = None
    scenery_custom_item_count: int | None = None
    has_custom_items: bool | None = None
    decoration_parse_status: str = "unparsed"

    parse_status: ParseStatus = ParseStatus.UNPARSED
    parse_error_code: ParseErrorCode | None = None
    parse_error_detail: str | None = None

    raw_artifact_path: str | None = None
    raw_artifact_hash: str | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class BlockPlacement:
    """One block on a map. Grid or free — see ``is_free``.

    Invariant (migration 010):
      is_free=False → x/y/z set, abs_*/yaw/pitch/roll None
      is_free=True  → x/y/z None, abs_x/abs_y/abs_z set, yaw/pitch/roll set
    """

    id: int | None
    map_id: int
    parser_version: str
    created_by_version: str
    source_artifact_ids: dict[str, str]

    block_family: str
    block_type: str
    placement_index: int

    x: int | None = None
    y: int | None = None
    z: int | None = None
    is_free: bool = False
    abs_x: Decimal | None = None
    abs_y: Decimal | None = None
    abs_z: Decimal | None = None
    yaw: float | None = None
    pitch: float | None = None
    roll: float | None = None

    variant: str | None = None
    rotation: int = 0
    flags: int | None = None
    surface: str | None = None
    raw_blob: dict[str, Any] | None = None
    created_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)
