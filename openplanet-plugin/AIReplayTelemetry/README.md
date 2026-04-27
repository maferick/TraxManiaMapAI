# AI Replay Telemetry — OpenPlanet plugin

Sibling of `AIRouteTelemetry`. Where `AIRouteTelemetry` answers the
boolean question "did the editor's AI driver finish the map?",
`AIReplayTelemetry` answers the much richer question "how was this
map actually driven?" by playing a replay back inside TM2020 and
sampling per-tick telemetry off the ghost player.

That gives the learning side of the stack the full motion / input
ground truth a finishability signal can't provide.

## Why this exists

The corpus has many maps with replays attached (clean and otherwise).
Under the **corpus-finishable axiom** + the **replay-ground-truth
learning contract** (see `docs/learning/corpus-finishable-axiom.md`
and CLAUDE.md), replays are authoritative for finishability
*evidence*. But raw replay GBX files only give us the recorded
inputs / interpolated trajectory at parse time — they don't tell us,
for example, what wheel-contact state the car was in 200ms before
hitting a checkpoint, or what the steering input was during a
specific corner.

Playing the replay inside the actual game and sampling its physics
state gives us that. The plugin is the bridge: rig server hands it a
`{map_file, replay_file}` job, plugin plays the ghost back, plugin
writes a per-tick frame array to the output JSON.

- **Tested target:** OpenPlanet 1.29.5, AngelScript 2.39 WIP
- **Game:** Trackmania (2020) — `CTrackMania` only
- **Plugin ID folder:** `AIReplayTelemetry`

## Install

1. Copy this folder to
   `%USERPROFILE%\OpenplanetNext\Plugins\AIReplayTelemetry\`.
   The folder must contain `info.toml` and `Main.as`.
2. Launch Trackmania with OpenPlanet.
3. Open the OP overlay (F3) → Plugin manager → confirm
   "AI Replay Telemetry" is enabled and shows version 0.1.0.
4. Open the OP console (F4) and grep for `[AIReplayTelemetry]` —
   you should see `rig folder: <...>\OpenplanetNext\PluginStorage\AIReplayTelemetry`.

## Rig folder

The plugin reads / writes exclusively under its own
`PluginStorage\AIReplayTelemetry` directory. On a typical Windows
install:

```
C:\Users\<you>\OpenplanetNext\PluginStorage\AIReplayTelemetry
```

This is **distinct** from `AIRouteTelemetry`'s rig folder by design —
the two plugins handle different job classes and the Linux server
routes by destination folder.

## Protocol

Same envelope as `AIRouteTelemetry` (`ai_rig_v1`); additive fields.

### Agent → Plugin (`<id>.in.json`)

```json
{
  "protocol": "ai_rig_v1",
  "job_id": 42,
  "run_id": "aa391b2f0476efa1",
  "map_file":    "C:\\Users\\you\\Documents\\Trackmania2020\\Maps\\AI-inbox\\map.Map.Gbx",
  "replay_file": "C:\\Users\\you\\Documents\\Trackmania2020\\Replays\\AI-inbox\\ghost.Replay.Gbx",
  "deadline_unix": 1714123456,
  "metadata": { "map_id": 1212, "replay_id": 8721 }
}
```

### Plugin → Agent (`<id>.out.json`)

```json
{
  "protocol": "ai_rig_v1",
  "job_id": 42,
  "run_id": "aa391b2f0476efa1",
  "load_success": true,
  "plugin_version": "replay-plugin-v0.1",
  "sample_period_ms": 50,
  "finished": true,
  "exit_reason": "finished",
  "frame_count": 1240,
  "frames": [
    {
      "t_ms": 0,
      "x": 802.5, "y": 18.0, "z": 528.0,
      "vx": 0.0, "vy": 0.0, "vz": 0.0,
      "yaw": 0.0, "pitch": 0.0, "roll": 0.0,
      "steer": 0.0, "gas": 0.0, "brake": 0.0,
      "wheel_contact": 1,
      "gear": 0, "rpm": 800,
      "cp_index": 0, "finished": 0
    },
    /* ...one frame per SAMPLE_PERIOD_MS... */
  ],
  /* v0.1 backcompat shims so the existing aggregator parses: */
  "spawn_ok": true,
  "validation_status": "Validated",
  "checkpoint_times_ms": [],
  "driven_cells": []
}
```

### Frame schema

| field | type | description |
|---|---|---|
| `t_ms` | int | game-clock ms relative to the first sampled frame |
| `x`,`y`,`z` | float | world position (TM2020 units, 1 cell ≈ 32 units) |
| `vx`,`vy`,`vz` | float | linear velocity (units/s) |
| `yaw`,`pitch`,`roll` | float | rotation (radians; roll is 0 in v0.1, see Main.as note) |
| `steer` | float | -1..1 |
| `gas` | float | 0..1 |
| `brake` | float | 0..1 (binary on most patches) |
| `wheel_contact` | int | 1 if any wheel grounded, 0 if airborne |
| `gear` | int | engine gear |
| `rpm` | int | engine rpm |
| `cp_index` | int | checkpoints passed so far |
| `finished` | int | 0/1, becomes 1 on finish-line frame |

## Lifecycle

```
Main() coroutine:
  loop:
    scan <plugin_storage>/*.in.json without matching *.out.json
    for each:
      parse → back-to-menu → PlayMap(map_file, ghost=replay_file)
      wait up to PLAYGROUND_OPEN_WAIT_SECONDS for GUIPlayer to surface
        → set load_success (or report titlepack / missing-resource error)
      sample loop, every SAMPLE_PERIOD_MS:
        capture pos / vel / rot / inputs / wheels / gear / rpm / cp / finished
        stop on RaceFinished, MAX_FRAMES, or PLAYBACK_WAIT_SECONDS
      back-to-menu
      write .out.json
    sleep SCAN_INTERVAL_MS
```

## Configuration

All thresholds are constants at the top of `Main.as`:

| const | default | meaning |
|---|---|---|
| `PLAYGROUND_OPEN_WAIT_SECONDS` | 60 | ceiling on PlayMap → playground transition |
| `PLAYBACK_WAIT_SECONDS` | 300 | ceiling on the entire playback |
| `SAMPLE_PERIOD_MS` | 50 | 20Hz sampling — change if you need higher fidelity |
| `MAX_FRAMES` | 8000 | hard cap on frames per job (sanity guard) |
| `SCAN_INTERVAL_MS` | 1000 | rig folder poll cadence |

Lower `SAMPLE_PERIOD_MS` to 10 (100Hz) for jump / boost analysis if
the rig server's per-job byte budget allows.

## Scope (v0.1)

✅ **In**
- Per-frame world position, velocity, rotation, inputs.
- Wheel-contact + gear + rpm.
- Race-clock + finish detection + checkpoint count.
- Same rig-folder protocol as `AIRouteTelemetry` so the Windows agent
  needs only a `replay_telemetry_dir` config addition.

❌ **Out (deferred)**
- Roll capture (CSmPlayer.ScriptAPI doesn't expose it on every patch;
  v0.1 logs 0).
- Input replay-fidelity sweep (we capture what the game's playback
  layer interpolates, not the byte-exact recorded inputs from the
  replay file; for byte-exact use the offline GBX replay parser).
- Multiple-ghost support (only one `GUIPlayer` is sampled).
- Camera state — we only need the car, not the spectator camera.

## Troubleshooting

| symptom | likely cause |
|---|---|
| `[AIReplayTelemetry] rig folder: ...` never appears | Plugin not enabled in OP plugin manager |
| Every job reports `load_error: playground did not surface` | `replay_file` path wrong, replay belongs to a different map, or the game needs a titlepack you don't have installed |
| Frames captured but `finished=0` and `exit_reason=playback_ended_unfinished` | Replay is incomplete (player rage-quit) or didn't reach a finish gate |
| `frame_count` near 1 and `wheel_contact` always 0 | The ghost's CSmPlayer.ScriptAPI isn't surfacing — check OP version (1.29.5+ required) |

## Versioning

`plugin_version` on every `.out.json` lets the Linux server A/B
between plugin iterations. Bumping semantics: any change to the
frame schema or to which ScriptAPI fields populate the frames bumps
the version.
