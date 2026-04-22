# clip-checkpoint reconnaissance notes

Repository: `bigbang1112-cz/clip-checkpoint`

## What this tool actually does

`clip-checkpoint` extracts checkpoint times from ghost data and builds a MediaTracker clip that renders checkpoint/lap/delta text and optional sound.

Primary extraction path:

1. Accepts replay(s), ghost(s), or clip(s) indirectly through GBX.NET tool API bindings.
2. For replays, constructor path uses `replay.GetGhosts()`.
3. In `Produce()`, uses `ghost.Checkpoints` as the core data source.
4. Outputs a `.Clip.Gbx` for visualization, not a telemetry dataset.

Key file:
- `ClipCheckpoint/ClipCheckpointTool.cs`

## TM2020-specific relevance

- Confirms that useful replay-aligned timing breadcrumbs can be read offline from TM2020 ghost artifacts: checkpoint crossing times.
- No direct position/rotation sample extraction.
- No evidence of hidden TM2020 telemetry decoding.

## Practical hook point

Best minimal hook in this repo:

- `ClipCheckpointTool.Produce()` just after reading `var checkpoints = ghost.Checkpoints;`

Add sidecar JSON export:

```json
{
  "ghost": "<uid/login>",
  "race_time_ms": 54321,
  "checkpoints": [
    {"idx": 1, "t_ms": 10543},
    {"idx": 2, "t_ms": 20111}
  ]
}
```

This would give durable alignment anchors for later interpolation or map-segment attribution.

## Limitations discovered

- Checkpoint granularity only; no high-frequency time-series.
- Cannot recover steering/accel between checkpoints by itself.
- Utility is strongest when fused with clip-input controls and map graph constraints.
