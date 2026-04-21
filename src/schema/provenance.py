"""Provenance entities: ingestion snapshots and stage runs.

Every derived row in every other table points back to these two.
Schema mirrors ``migrations/mariadb/001_ingestion_snapshots.sql`` and
``migrations/mariadb/002_stage_runs.sql``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class StageStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class IngestionSnapshot:
    snapshot_id: str
    source_system: str
    started_at: datetime
    user_agent: str
    rate_limit_rps: Decimal
    resolved_config_hash: str
    code_version: str
    completed_at: datetime | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("started_at", self.started_at), ("completed_at", self.completed_at)):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class StageRun:
    id: int | None
    stage: str
    stage_version: str
    started_at: datetime
    resolved_config_hash: str
    code_version: str
    input_ref: str
    status: StageStatus
    completed_at: datetime | None = None
    duration_ms: int | None = None
    output_summary: dict[str, Any] | None = None
    error_taxonomy_code: str | None = None
    error_message: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        if self.completed_at is not None and self.completed_at.tzinfo is None:
            raise ValueError("completed_at must be timezone-aware")
