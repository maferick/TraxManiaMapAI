# replay-viewer reconnaissance notes

Repository: `bigbang1112-cz/replay-viewer`

## Why inspected

Checked as a possible signal that someone already transforms TM2020 replay data into browser-friendly sequential telemetry.

## What it uses

- Loads `CGameCtnReplayRecord` and `replay.GetGhosts()`.
- For rendering motion, it requires `firstGhost.SampleData`.
- Timeline generation uses `ghost.SampleData.Samples` cast to `CSceneVehicleCar.Sample`.

Key file:
- `ReplayViewer/ReplayViewerToolComponent.razor`

## Meaning for TM2020 telemetry extraction

- This project depends on GBX.NET exposing `SampleData`; it does not implement its own low-level TM2020 decoder.
- If TM2020 replay/ghost parsing yields empty sample data in your pipeline, this tool does not provide an alternate decode path.
- It is therefore evidence of a consumer pipeline, not an independent extractor.

## Takeaway

Replay-viewer is not the missing offline decoder. It is useful only if upstream GBX.NET parsing already provides non-empty sample streams.
