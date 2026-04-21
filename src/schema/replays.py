"""Replay-related entities: canonical replay metadata + derived features.

Schema mirrors ``migrations/mariadb/005_replays.sql`` and
``migrations/mariadb/006_replay_features.sql``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from src.parsers.errors import ParseErrorCode, ParseStatus


class CleanStatus(str, Enum):
    UNPROCESSED = "unprocessed"
    CLEAN = "clean"
    USABLE_WITH_WARNINGS = "usable_with_warnings"
    REJECTED = "rejected"


class ReplayCohort(str, Enum):
    INTENT = "intent"
    PERFORMANCE = "performance"
    ROBUSTNESS = "robustness"


@dataclass(frozen=True)
class Replay:
    id: int | None
    source_system: str
    source_replay_id: str
    map_id: int
    ingestion_snapshot: str
    created_by_version: str

    player_login: str | None = None
    player_display_name: str | None = None
    finish_time_ms: int | None = None
    rank_metadata: dict[str, Any] | None = None

    clean_status: CleanStatus = CleanStatus.UNPROCESSED
    clean_version: str | None = None
    cohort_membership: frozenset[ReplayCohort] = frozenset()

    parse_status: ParseStatus = ParseStatus.UNPARSED
    parse_error_code: ParseErrorCode | None = None
    parse_error_detail: str | None = None

    raw_artifact_path: str | None = None
    raw_artifact_hash: str | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ReplayFeatures:
    id: int | None
    replay_id: int
    feature_extractor_version: str
    features: dict[str, Any]
    created_by_version: str
    source_artifact_ids: dict[str, str]
    diagnostics: dict[str, Any] | None = None
    created_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)
