# TM2020 replay telemetry reverse-engineering spike

## Scope

Focused reconnaissance on existing GitHub tooling for offline replay-aligned TM2020 breadcrumbs, not a full parser rewrite.

Inspected repositories:
- `BigBang1112/gbx-net`
- `bigbang1112-cz/clip-input`
- `bigbang1112-cz/clip-checkpoint`
- `bigbang1112-cz/replay-viewer` (follow-up)

## Parse-path trace (file load -> structured output)

### 1) Replay/Ghost/Clip load

All three tools rely on GBX.NET node parsing into:
- `CGameCtnReplayRecord`
- `CGameCtnGhost`
- `CGameCtnMediaClip`

For replay containers, consumer tools call `replay.GetGhosts()` and then read ghost substructures.

### 2) TM2020 input extraction path (working surface)

In GBX.NET:
- `CGameCtnGhost` chunk `0x01D` carries `PlayerInputData[]`.
- `CGameCtnGhost.PlayerInputData` decodes TM2020-specific bits into typed `IInput` events (e.g., `SteerTM2020`, `RespawnTM2020`).

In clip-input:
- It uses `ghost.PlayerInputs?.FirstOrDefault()?.Inputs` first, then fallbacks.
- It transforms those inputs into MediaTracker blocks.

**Conclusion:** existing ecosystem can produce **sequential control-event streams** from TM2020 ghost/replay artifacts when PlayerInputs are present.

### 3) Checkpoint extraction path (working surface)

In clip-checkpoint:
- Uses `ghost.Checkpoints` and emits checkpoint/lap/delta visual tracks.

**Conclusion:** existing ecosystem can provide replay-aligned **timing anchors** (checkpoint times), but not dense motion telemetry.

### 4) Motion sample path (currently blocked for your case)

In replay-viewer:
- Motion rendering depends on `ghost.SampleData.Samples`.
- No independent decoder exists there; it consumes GBX.NET output.

If GBX.NET yields empty TM2020 sample payloads in your environment, replay-viewer does not unlock new parsing capability.

## Assessment against requested decision matrix

### a) Directly decode hidden TM2020 replay structures?

- **Partially:** yes for TM2020 input bitstreams inside `PlayerInputData`.
- **No clear evidence** in these repos of a separate decoder for hidden/withheld TM2020 positional telemetry in Replay containers beyond what GBX.NET already exposes.

### b) Reconstruct useful time-series from partial structures?

- **Yes, practical:**
  - Ordered control events (`IInput` timeline from clip-input path)
  - Checkpoint time sequence (`ghost.Checkpoints` from clip-checkpoint path)
- **But not full vehicle trajectory samples** at frame/tick resolution.

### c) Depend on replay->clip or other intermediate transformation?

- Yes, both clip tools transform decoded ghost metadata into `.Clip.Gbx` visualization artifacts.
- This is a presentation transform, not a hidden physics decoder.

### d) Cannot help beyond inputs/checkpoints?

- For your blocked TM2020 replay telemetry case: **effectively yes**.
- These tools currently help with control + checkpoint breadcrumbs, not full telemetry samples needed for direct route replay reconstruction.

## Best practical offline attack surface

### Recommended hook #1 (highest value): clip-input sidecar JSON

Add export in `ClipInputTool.GetGhostsAndEndTime(...)` / `GenerateGhostInputs(...)` to dump normalized `IInput` timeline per ghost.

Why this is strong:
- Already handles source variability (`Replay.Gbx`, `Ghost.Gbx`, `Clip.Gbx`).
- Uses TM2020-aware decoded input types.
- Minimal implementation risk.

### Recommended hook #2: clip-checkpoint sidecar JSON

Add export in `ClipCheckpointTool.Produce()` after checkpoint extraction.

Why:
- Gives reliable race-phase anchors to pair with control timeline.
- Useful for segment-level inference and sanity checks.

### Optional hook #3: GBX.NET record-data reconnaissance

`CGameCtnReplayRecord.RecordData` (`CPlugEntRecordData`) is parsed and may contain additional entity timeline payloads, but in this spike there is no confirmed ready-made mapping to vehicle sample telemetry suitable for immediate production route inference.

## Recommendation for your pipeline decision

Given the current blocker (empty TM2020 telemetry samples), the most realistic near-term path is:

1. **Adopt offline breadcrumb mode** based on:
   - TM2020 `PlayerInputData` event timeline,
   - checkpoint timing sequence.
2. Attempt constrained route inference from these partial signals.
3. In parallel, prepare fallback:
   - **Openplanet runtime exporter** for authoritative per-tick motion telemetry if offline fidelity remains insufficient.

## Bottom line answer

Where does the ecosystem already turn TM2020 replay/ghost artifacts into reusable structured sequential data?

- **Yes:** in GBX.NET `CGameCtnGhost.PlayerInputData` -> `IInput` sequences (consumed by clip-input).
- **Yes:** in ghost checkpoint arrays -> timed checkpoint sequence (consumed by clip-checkpoint).
- **No (found):** an independent offline decoder in these repos that reliably yields full TM2020 replay-aligned motion telemetry samples when GBX.NET sample extraction is empty.
