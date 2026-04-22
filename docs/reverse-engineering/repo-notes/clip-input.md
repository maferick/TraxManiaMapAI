# clip-input reconnaissance notes

Repository: `bigbang1112-cz/clip-input`

## What this tool actually does

`clip-input` is a **consumer** of already-decoded GBX.NET input structures, not a raw TM2020 replay decoder.

Primary extraction path:

1. Tool entry points accept `CGameCtnReplayRecord`, `CGameCtnGhost`, or `CGameCtnMediaClip`.
2. For replay input, it calls `replay.GetGhosts()` and then picks inputs in this priority order:
   - `ghost.PlayerInputs?.FirstOrDefault()?.Inputs`
   - fallback `replay?.Inputs`
   - fallback `ghost.GetDisplayableInputs().ToList()`
3. It then transforms `IInput` events into MediaTracker tracks via `InputTrackBuilder` and builder classes.

Key files:
- `ClipInput/ClipInputTool.cs`
- `ClipInput/InputTrackBuilder.cs`
- `ClipInput/Builders/*`

## TM2020-specific relevance

- The tool claims TM2020 input support, but that support is grounded in GBX.NET's `CGameCtnGhost.PlayerInputData` decode pipeline.
- It does not appear to decode hidden replay-only telemetry samples (position/rotation/speed timeline).
- It can still provide replay-aligned **control-event sequences** when `PlayerInputs` exist in parsed ghosts.

This is useful as breadcrumbs for route inference if combined with checkpoints and map topology, but not a drop-in replacement for full telemetry.

## Practical hook point

Best minimal hook in this repo:

- `ClipInputTool.GetGhostsAndEndTime(...)` and `GenerateGhostInputs(...)`

Add an optional sidecar exporter there to emit JSON such as:

```json
{
  "source": "Replay.Gbx",
  "ghost": "<uid/login>",
  "input_version": "_2020_07_20",
  "inputs": [{"t_ms": 1230, "type": "SteerTM2020", "value": -64}]
}
```

Why here:
- Inputs are already normalized into ordered `IInput` records.
- Timing offsets (including `FakeIsRaceRunning`) are already handled.
- No parser rewrite required.

## Limitations discovered

- This repo does not expose or reconstruct `SampleData` from TM2020 replays.
- No evidence of replay->clip conversion that recreates physical trajectory.
- It is an input-visualization generator, not a motion telemetry extractor.
