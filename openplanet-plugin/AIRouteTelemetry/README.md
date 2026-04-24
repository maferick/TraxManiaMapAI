# AI Route Telemetry — OpenPlanet plugin

AngelScript plugin that closes the feedback loop of the remote-test
rig. Watches the shared rig folder for `<id>.in.json` triggers
dropped by the Windows agent, loads the referenced `.Map.Gbx` via
the TM2020 title script API, observes load-time errors + spawn
state, and writes `<id>.out.json` for the agent to ship upstream.

- **Tested target:** OpenPlanet 1.29.5, AngelScript 2.39 WIP
- **Game:** Trackmania (2020) — `CTrackMania` only
- **Plugin ID folder:** `AIRouteTelemetry` (matches the agent's
  `plugin_rig_dir` convention)

## Install

1. Copy this folder to `%USERPROFILE%\OpenplanetNext\Plugins\AIRouteTelemetry\`.
   The folder must contain `info.toml` and `Main.as`.
2. Launch Trackmania with OpenPlanet.
3. Open the OP overlay (F3) → Plugin manager → confirm
   "AI Route Telemetry" is enabled and shows version 0.1.0.
4. Open the OP console (F4) and grep for `[AIRouteTelemetry]` —
   you should see `rig folder: <...>\OpenplanetNext\PluginStorage\AIRouteTelemetry`.

## Rig folder

The plugin reads / writes exclusively under its own
`PluginStorage\AIRouteTelemetry` directory. On a typical Windows
install:

```
C:\Users\<you>\OpenplanetNext\PluginStorage\AIRouteTelemetry
```

Point the Windows agent's `paths.plugin_rig_dir` at that same
folder. The two processes never share any other state.

## Protocol v1

The plugin handles files matching this shape:

### Agent → Plugin (`<id>.in.json`)

```json
{
  "protocol": "ai_rig_v1",
  "job_id": 42,
  "run_id": "aa391b2f0476efa1",
  "map_file": "C:\\Users\\you\\Documents\\Trackmania2020\\Maps\\AI-inbox\\aa391b2f0476efa1.Map.Gbx",
  "deadline_unix": 1714123456,
  "metadata": { "base_map_id": 1212, "random_seed": 42 }
}
```

### Plugin → Agent (`<id>.out.json`)

```json
{
  "protocol": "ai_rig_v1",
  "job_id": 42,
  "run_id": "aa391b2f0476efa1",
  "load_success": true,
  "load_error": null,
  "spawn_ok": true,
  "finished": false,
  "checkpoint_times_ms": [],
  "driven_cells": [],
  "exit_reason": "observer_timeout",
  "plugin_version": "plugin-v0.1"
}
```

## Lifecycle

```
Main() coroutine:
  loop:
    scan <plugin_storage>/*.in.json without matching *.out.json
    for each:
      parse → back-to-menu → PlayMap(map_file, "", "")
      wait up to LOAD_WAIT_SECONDS for RootMap → set load_success
      observe OBSERVE_SECONDS for GameTerminal[0].ControlledPlayer → set spawn_ok
      back-to-menu
      write .out.json
    sleep SCAN_INTERVAL_MS
```

## Scope

✅ **In** — load-success / load-error detection, spawn sanity,
structured telemetry file.

❌ **Out (v0.1)** — simulated driving, checkpoint-time capture
during autonomous play, driven-cell sampling. The OpenPlanet
sandbox disallows simulating player input; autonomous finish
detection requires a future integration with a "bot driver"
plugin or an external input layer. For now, `spawn_ok` +
`load_success` + `absence of errors` is the load-time signal.

If the operator drives the map manually after the plugin loads it,
the in-game CP widget still records times — but the plugin won't
capture them in v0.1 (the GameTerminal CP callback integration is
a follow-up noted in `Main.as`).

## Troubleshooting

| symptom | likely cause |
|---|---|
| `[AIRouteTelemetry] rig folder: ...` never appears | Plugin not enabled in OP plugin manager |
| `.in.json` files pile up, no `.out.json` | TM2020 not fully started; watch for "title script API never ready within deadline" in console |
| Every job reports `load_error` | Map file path wrong (agent + plugin rig_dir mismatch) OR GBX references custom titlepack |
| `protocol mismatch` on valid-looking input | Agent and plugin on different protocol versions — align both to `ai_rig_v1` |

## Versioning

`plugin_version` on every `.out.json` lets the Linux server
A/B between plugin iterations. Bumping semantics: any change to
the `.out.json` shape or to which fields are populated bumps the
version.
