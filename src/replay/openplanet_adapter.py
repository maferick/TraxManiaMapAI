"""Convert AIReplayTelemetry rig output into canonical ReplayTelemetry.

The in-game plugin (``openplanet-plugin/AIReplayTelemetry``) samples a
ghost through ``CSmArenaRulesMode.Ghost_GetPosition``, which yields
POSITION ONLY. The canonical schema in :mod:`src.replay.telemetry`
requires velocity per sample, so this module differentiates position
with respect to the game clock. That is the only derived quantity here;
everything else is passed through or dropped.

Why velocity is not captured in-game
------------------------------------
A ghost exposes no velocity, input, gear or wheel-contact surface
through any typed Openplanet API. The rich per-frame struct
(``CSceneVehicleVisState``) has no typed accessor and is reachable only
through memory-offset hacks. Differentiating a 20 Hz position series is
accurate enough for route inference and does not couple this pipeline to
game-memory layout, which changes without warning across patches.

Clock alignment and the leading nulls
-------------------------------------
The plugin's ``t_ms`` counts from whenever its sampling loop started,
which is during the pre-race countdown. The ghost's own checkpoint
splits count from RACE START. ``start_frame_index`` is the plugin's
observed movement anchor that bridges the two.

Before the ghost is placed in the scene, ``Ghost_GetPosition`` returns
the origin ``(0, 0, 0)``. Those leading frames are nulls, not "the car
sitting at the start line", and differentiating across the boundary
where the ghost pops into the world manufactures an absurd velocity
(measured: 31,081 km/h against a real top speed of 305 km/h on the same
run). So this adapter TRIMS everything before the anchor and re-bases
``time_ms`` to zero at it, which also makes ``time_ms`` mean what the
canonical schema says it means: milliseconds from replay start.

When the anchor is ``-1`` the plugin could not observe movement. Then
nothing is trimmed, nothing is re-based, and checkpoints are left
unmapped rather than anchored to a guess: a wrong anchor would silently
misattribute every split to the wrong stretch of track. Such a capture
is not usable as gold-set ground truth and callers should say so.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from src.replay.telemetry import (
    TELEMETRY_SCHEMA_VERSION,
    ReplayTelemetry,
    SampleFrame,
)

_LOG = logging.getLogger(__name__)

RIG_PROTOCOL = "ai_rig_v1"


class RigOutputError(ValueError):
    """Raised when a rig ``.out.json`` cannot be adapted."""


def _require_frames(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    frames = payload.get("frames")
    if not isinstance(frames, list):
        raise RigOutputError("rig output has no 'frames' list")
    if not frames:
        raise RigOutputError(
            "rig output has zero frames "
            f"(exit_reason={payload.get('exit_reason')!r}, "
            f"load_error={payload.get('load_error')!r})"
        )
    for i, frame in enumerate(frames):
        if not isinstance(frame, Mapping):
            raise RigOutputError(f"frames[{i}] is not an object")
        for key in ("t_ms", "x", "y", "z"):
            if key not in frame:
                raise RigOutputError(f"frames[{i}] missing {key!r}")
    return frames


def _velocities(
    times_ms: Sequence[int],
    xs: Sequence[float],
    ys: Sequence[float],
    zs: Sequence[float],
) -> list[tuple[float, float, float]]:
    """Central-difference velocity in m/s.

    Endpoints use a one-sided difference. Samples whose neighbours share
    a timestamp get zero velocity rather than a division by zero; the
    game clock can repeat a value if two samples land inside one frame.
    """
    n = len(times_ms)
    if n == 1:
        return [(0.0, 0.0, 0.0)]

    out: list[tuple[float, float, float]] = []
    for i in range(n):
        lo = i - 1 if i > 0 else i
        hi = i + 1 if i < n - 1 else i
        dt_ms = times_ms[hi] - times_ms[lo]
        if dt_ms <= 0:
            out.append((0.0, 0.0, 0.0))
            continue
        scale = 1000.0 / dt_ms
        out.append(
            (
                (xs[hi] - xs[lo]) * scale,
                (ys[hi] - ys[lo]) * scale,
                (zs[hi] - zs[lo]) * scale,
            )
        )
    return out


def _checkpoint_indices(
    times_ms: Sequence[int],
    split_times_ms: Sequence[int],
    anchored: bool,
) -> tuple[int, ...]:
    """Map race-relative split times onto sample indices.

    Assumes ``times_ms`` has already been re-based so that index 0 is
    race start. Returns an empty tuple when there was no movement
    anchor to re-base against. See the module docstring for why
    guessing is worse than returning nothing.
    """
    if not anchored or not split_times_ms:
        return ()
    return tuple(
        min(range(len(times_ms)), key=lambda i: abs(times_ms[i] - int(split)))
        for split in split_times_ms
    )


def telemetry_from_rig_output(
    payload: Mapping[str, Any],
    *,
    source_replay_id: str,
) -> ReplayTelemetry:
    """Adapt one ``<job>.out.json`` document into a ReplayTelemetry.

    Raises :class:`RigOutputError` when the job did not produce usable
    frames. A failed job is not an exception in the pipeline sense: the
    caller decides whether to skip it or stop, so the error carries the
    plugin's own ``exit_reason`` and ``load_error``.
    """
    protocol = payload.get("protocol")
    if protocol != RIG_PROTOCOL:
        raise RigOutputError(
            f"unexpected protocol {protocol!r}, expected {RIG_PROTOCOL!r}"
        )
    if not payload.get("load_success"):
        raise RigOutputError(
            "rig job did not load: " + str(payload.get("load_error", "unknown"))
        )

    frames = _require_frames(payload)
    start_frame_index = int(payload.get("start_frame_index", -1))

    # Trim the pre-spawn nulls and re-base the clock to race start. See
    # the module docstring: skipping this manufactures a five-figure
    # km/h velocity at the spawn boundary.
    anchored = 0 <= start_frame_index < len(frames)
    if start_frame_index >= len(frames):
        _LOG.warning(
            "start_frame_index %d out of range (%d frames); "
            "treating capture as unanchored",
            start_frame_index,
            len(frames),
        )
    trimmed_lead_frames = start_frame_index if anchored else 0
    if anchored:
        frames = frames[start_frame_index:]

    times_ms = [int(f["t_ms"]) for f in frames]
    if anchored:
        origin = times_ms[0]
        times_ms = [t - origin for t in times_ms]
    xs = [float(f["x"]) for f in frames]
    ys = [float(f["y"]) for f in frames]
    zs = [float(f["z"]) for f in frames]

    velocities = _velocities(times_ms, xs, ys, zs)
    samples = tuple(
        SampleFrame(
            time_ms=times_ms[i],
            x=xs[i],
            y=ys[i],
            z=zs[i],
            vx=velocities[i][0],
            vy=velocities[i][1],
            vz=velocities[i][2],
        )
        for i in range(len(frames))
    )

    period_ms = int(payload.get("sample_period_ms") or 0)
    if period_ms <= 0:
        raise RigOutputError(
            f"rig output has unusable sample_period_ms={period_ms!r}"
        )
    sample_rate_hz = max(1, round(1000 / period_ms))

    ghost = payload.get("ghost")
    ghost = ghost if isinstance(ghost, Mapping) else {}
    split_times = ghost.get("checkpoint_times_ms") or ()

    checkpoint_indices = _checkpoint_indices(times_ms, split_times, anchored)
    if split_times and not checkpoint_indices:
        _LOG.warning(
            "replay %s: %d checkpoint splits present but no movement "
            "anchor, leaving them unmapped",
            source_replay_id,
            len(split_times),
        )

    finished = bool(payload.get("finished"))
    ghost_time = ghost.get("time_ms")
    finish_time_ms = int(ghost_time) if finished and ghost_time else None

    # The ghost reports a respawn COUNT but not respawn times, so
    # restart_sample_indices stays empty. The count is preserved in
    # `extra` so a downstream teleport-rule hit can be checked against
    # it: n respawns should produce n position discontinuities.
    extra: dict[str, Any] = {
        "source": "openplanet_ai_replay_telemetry",
        "plugin_version": payload.get("plugin_version"),
        "exit_reason": payload.get("exit_reason"),
        "start_frame_index": start_frame_index,
        "trimmed_lead_frames": trimmed_lead_frames,
        "clock_rebased_to_race_start": anchored,
        "ghost_nickname": ghost.get("nickname"),
        "ghost_nb_respawns": ghost.get("nb_respawns"),
        "ghost_checkpoint_times_ms": list(split_times),
        "velocity_is_differentiated": True,
    }

    return ReplayTelemetry(
        schema_version=TELEMETRY_SCHEMA_VERSION,
        source_replay_id=source_replay_id,
        sample_rate_hz=sample_rate_hz,
        samples=samples,
        # A ghost exposes Nickname but not login. Leaving this None is
        # correct; the nickname is in `extra` and must not be passed off
        # as a login, which downstream code uses as a stable identity.
        player_login=None,
        finish_time_ms=finish_time_ms,
        checkpoint_sample_indices=checkpoint_indices,
        restart_sample_indices=(),
        extra=extra,
    )


def telemetry_to_dict(telemetry: ReplayTelemetry) -> dict[str, Any]:
    """Serialise back to the wire shape ``telemetry.from_dict`` accepts."""
    return {
        "schema_version": telemetry.schema_version,
        "source_replay_id": telemetry.source_replay_id,
        "sample_rate_hz": telemetry.sample_rate_hz,
        "player_login": telemetry.player_login,
        "finish_time_ms": telemetry.finish_time_ms,
        "checkpoint_sample_indices": list(telemetry.checkpoint_sample_indices),
        "restart_sample_indices": list(telemetry.restart_sample_indices),
        "extra": dict(telemetry.extra),
        "samples": [
            {
                "time_ms": s.time_ms,
                "x": s.x,
                "y": s.y,
                "z": s.z,
                "vx": s.vx,
                "vy": s.vy,
                "vz": s.vz,
            }
            for s in telemetry.samples
        ],
    }
