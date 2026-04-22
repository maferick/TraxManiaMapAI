"""Breadcrumb payload — the offline-derived IInput timeline +
checkpoint times the wrapper emits when GBX.NET can't decode a
replay's position samples.

Parallel to :mod:`src.replay.telemetry`. Breadcrumbs carry event-level
driving evidence (Accelerate / Brake / SteerTM2020 / Respawn /
MouseAccu) and race-phase anchors (checkpoint_times_ms), which is
enough for a subset of cleaning rules to run without continuous
(x, y, z) samples.

Sidecar shape (written by `parsers/gbx-wrapper/ReplayParser.cs`):

    {
      "schema_version": 1,
      "source_replay_id": "12345",
      "player_login": "...",
      "finish_time_ms": 184238,
      "checkpoint_times_ms": [50036, 86295, 130212, 156773, 184238],
      "inputs": [
          {"time_ms": -1580, "kind": "MouseAccu", "repr": "..."},
          {"time_ms": 0, "kind": "Accelerate", "repr": "Accelerate { ... Pressed = True }"},
          ...
      ],
      "inputs_count": 5336
    }

Downstream cleaning rules read this via :class:`FileBreadcrumbLoader`.
Rules that need position samples stay on the telemetry path; rules
that work from events + timing (restart, spectator, incomplete,
invalid_timing) have breadcrumb-aware equivalents under
``src.replay.rules.breadcrumb``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

BREADCRUMBS_SCHEMA_VERSION = 1


class BreadcrumbsFormatError(ValueError):
    """Raised when a breadcrumb payload fails schema validation."""


class BreadcrumbsLoadError(RuntimeError):
    """Raised when the sidecar cannot be read or parsed."""


@dataclass(frozen=True)
class InputEvent:
    time_ms: int
    kind: str
    repr: str


@dataclass(frozen=True)
class ReplayBreadcrumbs:
    schema_version: int
    source_replay_id: str
    inputs: tuple[InputEvent, ...]
    inputs_count: int
    player_login: str | None = None
    finish_time_ms: int | None = None
    checkpoint_times_ms: tuple[int, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != BREADCRUMBS_SCHEMA_VERSION:
            raise BreadcrumbsFormatError(
                f"unsupported breadcrumbs schema_version {self.schema_version}; "
                f"this build expects {BREADCRUMBS_SCHEMA_VERSION}"
            )
        if self.inputs_count < 0:
            raise BreadcrumbsFormatError(
                f"inputs_count must be >= 0, got {self.inputs_count}"
            )

    @property
    def duration_ms(self) -> int | None:
        """Race duration if breadcrumbs carry a start-to-finish span.

        Prefers ``finish_time_ms``; falls back to the last checkpoint
        time; returns ``None`` when neither is populated.
        """
        if self.finish_time_ms is not None:
            return self.finish_time_ms
        if self.checkpoint_times_ms:
            return self.checkpoint_times_ms[-1]
        return None

    def count_inputs_by_kind(self, kind: str) -> int:
        return sum(1 for inp in self.inputs if inp.kind == kind)


def _as_int(value: Any, *, field_name: str) -> int:
    if value is None:
        raise BreadcrumbsFormatError(f"{field_name} is required")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BreadcrumbsFormatError(
            f"{field_name} must be an integer, got {value!r}"
        ) from exc


def from_dict(payload: Mapping[str, Any]) -> ReplayBreadcrumbs:
    for key in ("schema_version", "source_replay_id", "inputs_count"):
        if key not in payload:
            raise BreadcrumbsFormatError(f"breadcrumbs payload missing '{key}'")
    raw_inputs = payload.get("inputs", [])
    if not isinstance(raw_inputs, list):
        raise BreadcrumbsFormatError("inputs must be a list")
    inputs: list[InputEvent] = []
    for i, raw in enumerate(raw_inputs):
        if not isinstance(raw, Mapping):
            raise BreadcrumbsFormatError(f"inputs[{i}] is not an object")
        time_val = raw.get("time_ms")
        inputs.append(
            InputEvent(
                time_ms=int(time_val) if time_val is not None else 0,
                kind=str(raw.get("kind", "")),
                repr=str(raw.get("repr", "")),
            )
        )
    checkpoint_list = payload.get("checkpoint_times_ms", [])
    if not isinstance(checkpoint_list, list):
        raise BreadcrumbsFormatError("checkpoint_times_ms must be a list")
    checkpoints = tuple(int(t) for t in checkpoint_list)
    finish_raw = payload.get("finish_time_ms")
    return ReplayBreadcrumbs(
        schema_version=_as_int(payload["schema_version"], field_name="schema_version"),
        source_replay_id=str(payload["source_replay_id"]),
        player_login=(str(payload["player_login"]) if payload.get("player_login") else None),
        finish_time_ms=(int(finish_raw) if finish_raw is not None else None),
        checkpoint_times_ms=checkpoints,
        inputs=tuple(inputs),
        inputs_count=_as_int(payload["inputs_count"], field_name="inputs_count"),
    )


class FileBreadcrumbLoader:
    """Reads ``<raw_artifact_path>.breadcrumbs.json`` emitted by the wrapper.

    Missing sidecars raise :class:`BreadcrumbsLoadError` — the caller
    (the clean pipeline) treats that as "breadcrumbs unavailable" and
    decides whether to fall through to telemetry_unavailable rejection
    or not.
    """

    def load_by_path(self, raw_artifact_path: str) -> ReplayBreadcrumbs:
        path = Path(raw_artifact_path + ".breadcrumbs.json")
        if not path.is_file():
            raise BreadcrumbsLoadError(f"breadcrumbs sidecar missing: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BreadcrumbsLoadError(f"{path} is not valid JSON: {exc}") from exc
        try:
            return from_dict(payload)
        except BreadcrumbsFormatError as exc:
            raise BreadcrumbsLoadError(f"{path} failed format validation: {exc}") from exc
