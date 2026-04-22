"""Tiny builders for ReplayBreadcrumbs fixtures, so tests read as
declarative scenarios. Mirrors tests/unit/_telemetry_builders.py."""
from __future__ import annotations

from src.replay.breadcrumbs import InputEvent, ReplayBreadcrumbs


def make_breadcrumbs(
    *,
    source_replay_id: str = "test-replay",
    player_login: str | None = "tester",
    finish_time_ms: int | None = 60_000,
    checkpoint_times_ms: tuple[int, ...] = (10_000, 25_000, 45_000, 60_000),
    inputs: tuple[InputEvent, ...] | None = None,
    inputs_count: int | None = None,
) -> ReplayBreadcrumbs:
    """Build a ReplayBreadcrumbs with sensible defaults.

    If ``inputs`` is omitted, a synthetic 150-event timeline is
    generated at 400 ms intervals (0.4 events/sec is well above the
    default spectator threshold of 1 input/sec when scaled to the
    60s default duration) — callers override per-scenario.
    """
    if inputs is None:
        # 150 events across 60_000 ms = 2.5/sec — above spectator threshold
        inputs = tuple(
            InputEvent(time_ms=i * 400, kind="SteerTM2020", repr=f"step{i}")
            for i in range(150)
        )
    if inputs_count is None:
        inputs_count = len(inputs)
    return ReplayBreadcrumbs(
        schema_version=1,
        source_replay_id=source_replay_id,
        player_login=player_login,
        finish_time_ms=finish_time_ms,
        checkpoint_times_ms=checkpoint_times_ms,
        inputs=inputs,
        inputs_count=inputs_count,
    )


def with_respawns(bc: ReplayBreadcrumbs, count: int) -> ReplayBreadcrumbs:
    """Clone ``bc`` with ``count`` Respawn events appended to the inputs."""
    extra = tuple(
        InputEvent(time_ms=60_000 + i * 100, kind="Respawn", repr="respawn")
        for i in range(count)
    )
    merged = bc.inputs + extra
    return ReplayBreadcrumbs(
        schema_version=bc.schema_version,
        source_replay_id=bc.source_replay_id,
        player_login=bc.player_login,
        finish_time_ms=bc.finish_time_ms,
        checkpoint_times_ms=bc.checkpoint_times_ms,
        inputs=merged,
        inputs_count=len(merged),
    )
