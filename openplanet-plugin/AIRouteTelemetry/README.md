# AI Route Telemetry — OpenPlanet plugin

AngelScript plugin that closes the feedback loop of the remote-test
rig. Watches the shared rig folder for `<id>.in.json` triggers
dropped by the Windows agent, opens the referenced `.Map.Gbx`
**in the TM2020 map editor**, runs the game's native AI validator
via `CGameEditorPluginMap.Validate()`, and writes `<id>.out.json`
with structured telemetry the agent ships upstream.

**v0.2 — unattended finishability via the editor validator.** TM2020
ships the same AI driver the TMX upload path uses; the plugin
delegates the "can the map be driven from spawn to goal?" question
to the game itself instead of trying to simulate input.

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
  "finished": true,
  "validation_status": "Validated",
  "author_time_ms": 18450,
  "checkpoint_times_ms": [],
  "driven_cells": [],
  "exit_reason": "validated",
  "plugin_version": "plugin-v0.2"
}
```

**New in v0.2:**

- `validation_status` — `Validated` / `Validable` / `NotValidable` /
  `Unknown`. Result of `CGameEditorPluginMap.ValidationStatus` after
  `Validate()` runs. `Validated` means the AI successfully drove
  from spawn to goal.
- `author_time_ms` — author-medal time the in-game validator set
  on the map, or absent when validation didn't succeed.
- `finished` is now derived from `validation_status == "Validated"`
  (the map is finishable by the game's own standards).

Protocol version stays `ai_rig_v1` — v0.1 plugin files still parse.

## Lifecycle (v0.2)

```
Main() coroutine:
  loop:
    scan <plugin_storage>/*.in.json without matching *.out.json
    for each:
      parse → back-to-menu → EditMap(map_file, "", "")
      wait up to EDITOR_OPEN_WAIT_SECONDS for editor.PluginMapType.IsEditorReadyForRequest
         → set load_success (or report titlepack / missing-resource error)
      PluginMapType.Validate()
      wait up to VALIDATE_WAIT_SECONDS polling ValidationStatus
         → capture final state (Validated / NotValidable / Validable)
         → pull PluginMapType.Map.TMObjective_AuthorTime if Validated
      back-to-menu
      write .out.json
    sleep SCAN_INTERVAL_MS
```

## Scope (v0.2)

✅ **In**
- Map-load error detection (titlepack / missing resources /
  corrupt GBX — all surface as "editor did not open within
  EDITOR_OPEN_WAIT_SECONDS")
- **Native editor validation** — the game's AI driver attempts
  spawn → goal and reports finishability + author-medal time.
  No human input, no OP input-simulation gymnastics.

❌ **Out (deferred)**
- Per-checkpoint driven telemetry during live play (the native
  validator reports only the overall result; to break it down
  per CP we'd need to wrap it or add a second pass that loads the
  validator's ghost replay)
- Driven-cell sampling (same reason; plugin writes empty arrays
  for backcompat with v0.1 protocol)

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
