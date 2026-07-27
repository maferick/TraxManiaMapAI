# Workstream — OpenPlanet runtime telemetry exporter

## Status

**Capture works, 2026-07-27.** The exporter runs and produces validated
per-frame trajectories. What is delivered:

- `openplanet-plugin/AIReplayTelemetry` v0.2 plays a map's own author
  ghost and samples its position every 50ms.
- `src/replay/openplanet_adapter.py` converts rig output into the
  canonical `ReplayTelemetry` shape, differentiating velocity.
- `tools/capture_replay_telemetry.py` drives single jobs or a JSONL
  batch.
- `tests/unit/test_openplanet_adapter.py`, 11 offline tests.

**Validation**: a 20-map pilot on the Stadium2020 linked-checkpoint gold
set gave 17/20 exact agreement and **zero disagreements** against the
offline GBX.NET metadata, matching finish time and checkpoint count to
the millisecond. The 3 losses are maps whose race never starts
(`player_seen=true, race_time=0ms`), not capture failures.

**Corpus reality check**: of 763 `LinkedCheckpoint` maps only 545 are
Stadium2020, and only 334 of those embed an author ghost. The claim that
every published map carries one is false.

## Cohort selection funnel

Every exclusion between the linked-checkpoint set and the capture
manifest, so the representativeness of any result computed on the cohort
can be judged rather than assumed.

| stage | maps | excluded | reason |
|---|---|---|---|
| `LinkedCheckpoint` maps | 763 | | the gold set as originally scoped |
| Stadium2020 only | 545 | 218 | other TM games; TMX hosts several |
| artifact present on disk | 545 | 0 | all resolved |
| `diagnose-map` parsed | 544 | 1 | one unparseable artifact |
| embeds an author ghost | 334 | 210 | no validation ghost to drive |
| already captured (pilot) | 314 | 20 | in the manifest as a separate batch |

So the bulk manifest is **314 maps, 61.3% of the Stadium2020
linked-checkpoint population**, and the dominant exclusion by far is the
absence of an embedded author ghost (210 maps, 38.5%). That exclusion is
not random with respect to anything we have checked, and nothing yet
rules out that ghost-less maps differ systematically from ghosted ones.
Treat cohort results as representative of *ghosted* linked-checkpoint
Stadium2020 maps, not of the corpus.

**The 20-map pilot is deliberately NOT representative.** It was drawn
from a 15-60s band spread evenly across that band, to fit a sitting.
The full 334 run 2s to 25,811s (p25 20s, p50 34s, p75 52s, p95 501s), so
the pilot omits both the very short and the long tail. Distribution
statistics must come from the bulk run, not the pilot.

The bulk manifest is ordered by race time ascending so that it is
stoppable, but that ordering makes any early stop
**systematically biased toward short tracks**. A partial bulk run is not
a valid sample of the cohort.

Still open, in order:

1. Coordinate reconciliation. Captures are in metres; `block_placements`
   is in grid cells. This is called out under Risks below and is now the
   load-bearing unknown, because route inference cannot start until a
   trajectory can be resolved to blocks.
2. Ingestion. The `ingest-openplanet-telemetry` command and the
   `replays.openplanet_telemetry_path` column described below are NOT
   built; artifacts are currently loose JSON.
3. Bulk capture of the remaining ~314 ghosted maps.

The offline GBX.NET path still cannot decode position samples, which is
why in-game playback is required at all. See
`docs/reverse-engineering/tm2020-replay-telemetry-spike.md`.

Parking this in its own file — rather than folding it into an existing
PR — because it is:

- **process-isolated** from the Python / wrapper pipeline. The
  exporter is AngelScript running inside the game client; its outputs
  land on disk and are ingested through a separate adapter.
- **long-lived.** Authoring the plugin, iterating on sample fidelity,
  and hardening capture against game-update drift is weeks of work on
  a different surface than the main pipeline.
- **independent of the main critical path.** PR 5 scaffold, PR 6
  constraint graph, and PR 7 dry-run can all progress on what's
  decodable today (breadcrumbs, block placements, adjacency evidence).

## Goal

Produce authoritative TM2020 per-tick motion telemetry:

- position `(x, y, z)` in map coordinates
- velocity `(vx, vy, vz)`
- timestamp (ms since race start)
- checkpoint crossings (index, wall time)
- final finish time

At native game tick resolution (50 Hz typical), exported during normal
play or during a replay watched from inside the game client.

## Non-goals

- reverse-engineering the offline GBX entity-record format. That's a
  separate, uncertain workstream; this one bypasses it.
- replacing the existing replay ingestion path. Breadcrumbs + TMX
  metadata continue to flow through the offline wrapper. The exporter
  is an enrichment source, not a replacement.
- real-time / online streaming. File-based handoff is sufficient.

## Interface to the rest of the system

The exporter produces a JSON artifact per captured run, one per
replay, in a schema compatible with
`src/replay/telemetry.py::ReplayTelemetry`. Fields:

| Field                        | Source                                |
|------------------------------|---------------------------------------|
| `schema_version`             | constant `1` (same as wrapper path)   |
| `source_replay_id`           | author-chosen stable id               |
| `sample_rate_hz`             | actual tick rate observed             |
| `player_login`               | in-game login                         |
| `finish_time_ms`             | race finish timestamp                 |
| `samples[].{time_ms,x,y,z,vx,vy,vz}` | per-tick state (non-empty)    |
| `checkpoint_sample_indices`  | indices into `samples`                |
| `checkpoint_times_ms`        | wall-time of each checkpoint crossing |

Ingestion entry point: a new CLI command, tentatively
`ingest-openplanet-telemetry`, that reads one or more JSON files and
writes them through the existing telemetry-sidecar path alongside the
raw `.Replay.Gbx` artifact the run corresponds to. The
`replays.breadcrumbs_path` column stays untouched — breadcrumbs remain
the wrapper's responsibility. A new column
(`replays.openplanet_telemetry_path` or similar) pins the enriched
artifact.

Match to a specific replay row is done via `(map_uid, player_login,
finish_time_ms)` within the same `ingestion_snapshot`. Rows where no
replay exists yet accept the telemetry and open the door for later
replay backfill.

## Success criteria

The workstream is done — for Phase 1 substrate purposes — when:

1. The exporter runs on a current retail TM2020 client without
   crashing under sustained play.
2. A captured set of ≥100 replays on a known benchmark map yields
   telemetry artifacts that pass `from_dict` validation and the
   full replay-cleaning rule stack (teleport, outlier_speed,
   zero_motion, restart, spectator) without false rejections on
   known-clean driving.
3. Route inference (PR 5) on that set produces a non-trivial
   centerline — visually inspected against the map layout — on at
   least one fixture map.

That is the minimum bar to claim we have an end-to-end TM2020 route
substrate. No surrogate, model, or benchmark claim rests on the
exporter before those criteria are met.

## Risks

- **Game-update drift.** Nadeo ships client updates that can break
  AngelScript plugin APIs. The workstream owner must treat
  compatibility maintenance as ongoing, not one-shot.
- **Sample-rate variability.** Game ticks are nominally 50 Hz but
  frame-rate-coupled. Artifacts must record the observed rate, and
  downstream code must not assume uniform spacing.
- **Coordinate-system mismatch.** TM2020 internal coordinates may
  differ from the block-grid coordinates used in `block_placements`.
  Reconciling the two (a one-time calibration) is part of this
  workstream's output.
- **Capture bias.** Running the exporter only during the author's own
  play sessions would produce a single-player population; downstream
  clustering needs a diverse set. Plan for multi-player captures or
  replay-watching from within the client.

## Handoff checklist

When picking this up, start from:

1. `docs/reverse-engineering/tm2020-replay-telemetry-spike.md` —
   confirms the offline path can't supply this data, and that
   recommendation #1 (clip-input breadcrumbs) is already landed in
   the wrapper.
2. `parsers/gbx-wrapper/ReplayParser.cs` — current breadcrumb export
   shape, for reference on what the exporter should produce
   *differently* (per-tick samples, not per-event inputs).
3. `src/replay/telemetry.py` — the target schema.
4. `src/replay/pipeline.py` — the cleaner the exporter output will
   flow through. Its rules define what "usable" means.

First deliverable should be a minimal exporter that captures one
race from one map and writes one JSON file that passes `from_dict`
validation. Everything else is iteration on fidelity and scale.
