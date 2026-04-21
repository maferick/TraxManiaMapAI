"""Small builders for synthetic ReplayTelemetry objects used in tests."""
from __future__ import annotations

from typing import Iterable

from src.replay.telemetry import (
    TELEMETRY_SCHEMA_VERSION,
    ReplayTelemetry,
    SampleFrame,
)


def make_telemetry(
    *,
    duration_ms: int = 30_000,
    sample_rate_hz: int = 50,
    start_xyz: tuple[float, float, float] = (0.0, 0.0, 0.0),
    straight_speed_mps: float = 30.0,
    finished: bool = True,
    restart_indices: Iterable[int] = (),
    checkpoint_indices: Iterable[int] = (),
    source_replay_id: str = "test-replay",
) -> ReplayTelemetry:
    """Build a well-formed replay moving in a straight line along +x.

    Tests mutate the returned ``samples`` tuple (by rebuilding) to
    inject rule-triggering anomalies.
    """
    period_ms = 1000 // sample_rate_hz
    count = max(2, duration_ms // period_ms + 1)
    samples: list[SampleFrame] = []
    x, y, z = start_xyz
    dx_per_tick = straight_speed_mps * (period_ms / 1000.0)
    for i in range(count):
        samples.append(
            SampleFrame(
                time_ms=i * period_ms,
                x=x + i * dx_per_tick,
                y=y,
                z=z,
                vx=straight_speed_mps,
                vy=0.0,
                vz=0.0,
            )
        )
    return ReplayTelemetry(
        schema_version=TELEMETRY_SCHEMA_VERSION,
        source_replay_id=source_replay_id,
        sample_rate_hz=sample_rate_hz,
        samples=tuple(samples),
        finish_time_ms=(count - 1) * period_ms if finished else None,
        checkpoint_sample_indices=tuple(checkpoint_indices),
        restart_sample_indices=tuple(restart_indices),
    )


def with_samples(
    base: ReplayTelemetry,
    samples: Iterable[SampleFrame],
    *,
    finished: bool | None = None,
    checkpoints: Iterable[int] | None = None,
    restarts: Iterable[int] | None = None,
) -> ReplayTelemetry:
    """Return a copy of ``base`` with the sample list replaced."""
    sample_tuple = tuple(samples)
    return ReplayTelemetry(
        schema_version=base.schema_version,
        source_replay_id=base.source_replay_id,
        sample_rate_hz=base.sample_rate_hz,
        samples=sample_tuple,
        player_login=base.player_login,
        finish_time_ms=base.finish_time_ms if finished is None else (
            sample_tuple[-1].time_ms if finished else None
        ),
        checkpoint_sample_indices=(
            tuple(checkpoints) if checkpoints is not None else base.checkpoint_sample_indices
        ),
        restart_sample_indices=(
            tuple(restarts) if restarts is not None else base.restart_sample_indices
        ),
    )
