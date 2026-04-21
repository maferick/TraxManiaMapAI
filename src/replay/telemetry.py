"""Canonical replay telemetry schema.

This is the contract the GBX wrapper must produce. The wrapper is
external; PR 4 ships cleaning rules that run against this shape. When
the wrapper lands, its output MUST conform to this schema — any
additive change bumps :data:`TELEMETRY_SCHEMA_VERSION`.

Units
-----
- ``time_ms``: milliseconds from replay start (integer).
- ``x/y/z``: world position in meters.
- ``vx/vy/vz``: velocity in m/s (NOT km/h; conversions live in the rules).
- ``finish_time_ms``: total elapsed replay time in milliseconds, or
  ``None`` if the replay did not reach the finish.
- ``sample_rate_hz``: the wrapper's *intended* sampling rate. The
  invalid-timing rule uses this to detect gaps.

A wrapper may emit samples at a higher resolution than declared
(e.g. variable-rate physics); ``sample_rate_hz`` is the contract, not
a measurement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

TELEMETRY_SCHEMA_VERSION = 1


class TelemetryFormatError(ValueError):
    """Raised when a payload cannot be loaded as a ReplayTelemetry."""


@dataclass(frozen=True)
class SampleFrame:
    time_ms: int
    x: float
    y: float
    z: float
    vx: float
    vy: float
    vz: float

    @property
    def speed_mps(self) -> float:
        return (self.vx * self.vx + self.vy * self.vy + self.vz * self.vz) ** 0.5


@dataclass(frozen=True)
class ReplayTelemetry:
    schema_version: int
    source_replay_id: str
    sample_rate_hz: int
    samples: tuple[SampleFrame, ...]
    player_login: str | None = None
    finish_time_ms: int | None = None
    checkpoint_sample_indices: tuple[int, ...] = ()
    restart_sample_indices: tuple[int, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != TELEMETRY_SCHEMA_VERSION:
            raise TelemetryFormatError(
                f"unsupported telemetry schema_version {self.schema_version}; "
                f"this build expects {TELEMETRY_SCHEMA_VERSION}"
            )
        if self.sample_rate_hz <= 0:
            raise TelemetryFormatError("sample_rate_hz must be positive")
        if not self.samples:
            raise TelemetryFormatError("samples must be non-empty")
        for index in self.checkpoint_sample_indices:
            if not 0 <= index < len(self.samples):
                raise TelemetryFormatError(
                    f"checkpoint_sample_indices entry {index} out of bounds "
                    f"(have {len(self.samples)} samples)"
                )
        for index in self.restart_sample_indices:
            if not 0 <= index < len(self.samples):
                raise TelemetryFormatError(
                    f"restart_sample_indices entry {index} out of bounds "
                    f"(have {len(self.samples)} samples)"
                )

    @property
    def duration_ms(self) -> int:
        return self.samples[-1].time_ms - self.samples[0].time_ms

    @property
    def finished(self) -> bool:
        return self.finish_time_ms is not None


def _as_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise TelemetryFormatError(f"{field_name} must be int, not bool")
    if not isinstance(value, int):
        raise TelemetryFormatError(
            f"{field_name} must be int, got {type(value).__name__}"
        )
    return value


def _as_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise TelemetryFormatError(f"{field_name} must be float, not bool")
    if not isinstance(value, (int, float)):
        raise TelemetryFormatError(
            f"{field_name} must be numeric, got {type(value).__name__}"
        )
    return float(value)


def _sample_from_mapping(raw: Mapping[str, Any], *, index: int) -> SampleFrame:
    required = ("time_ms", "x", "y", "z", "vx", "vy", "vz")
    for key in required:
        if key not in raw:
            raise TelemetryFormatError(
                f"sample[{index}] missing required field {key!r}"
            )
    return SampleFrame(
        time_ms=_as_int(raw["time_ms"], field_name=f"sample[{index}].time_ms"),
        x=_as_float(raw["x"], field_name=f"sample[{index}].x"),
        y=_as_float(raw["y"], field_name=f"sample[{index}].y"),
        z=_as_float(raw["z"], field_name=f"sample[{index}].z"),
        vx=_as_float(raw["vx"], field_name=f"sample[{index}].vx"),
        vy=_as_float(raw["vy"], field_name=f"sample[{index}].vy"),
        vz=_as_float(raw["vz"], field_name=f"sample[{index}].vz"),
    )


def from_dict(payload: Mapping[str, Any]) -> ReplayTelemetry:
    """Parse + validate a telemetry payload. Strict on required fields."""
    for key in ("schema_version", "source_replay_id", "sample_rate_hz", "samples"):
        if key not in payload:
            raise TelemetryFormatError(f"missing required top-level field {key!r}")

    raw_samples = payload["samples"]
    if not isinstance(raw_samples, list):
        raise TelemetryFormatError("samples must be a list")
    samples = tuple(
        _sample_from_mapping(s, index=i) if isinstance(s, Mapping) else _bad(i)
        for i, s in enumerate(raw_samples)
    )

    finish_raw = payload.get("finish_time_ms")
    finish_time_ms = (
        _as_int(finish_raw, field_name="finish_time_ms") if finish_raw is not None else None
    )

    checkpoints = tuple(
        _as_int(i, field_name=f"checkpoint_sample_indices[{k}]")
        for k, i in enumerate(payload.get("checkpoint_sample_indices", ()))
    )
    restarts = tuple(
        _as_int(i, field_name=f"restart_sample_indices[{k}]")
        for k, i in enumerate(payload.get("restart_sample_indices", ()))
    )

    player_login_raw = payload.get("player_login")
    player_login = str(player_login_raw) if player_login_raw is not None else None

    extra = payload.get("extra")
    if extra is not None and not isinstance(extra, dict):
        raise TelemetryFormatError("extra must be a mapping if present")

    return ReplayTelemetry(
        schema_version=_as_int(payload["schema_version"], field_name="schema_version"),
        source_replay_id=str(payload["source_replay_id"]),
        sample_rate_hz=_as_int(payload["sample_rate_hz"], field_name="sample_rate_hz"),
        samples=samples,
        player_login=player_login,
        finish_time_ms=finish_time_ms,
        checkpoint_sample_indices=checkpoints,
        restart_sample_indices=restarts,
        extra=dict(extra) if extra else {},
    )


def _bad(index: int) -> SampleFrame:  # pragma: no cover - helper raises
    raise TelemetryFormatError(f"sample[{index}] must be a mapping")
