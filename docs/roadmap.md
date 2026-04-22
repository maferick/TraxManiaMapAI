# Roadmap

Phase 1 roadmap. Scope = substrate (data + evaluation), not generation.

## PR sequence

### PR 1 — Repository bootstrap *(in progress)*

- repo structure
- Docker Compose stub for MariaDB + Neo4j
- config loader scaffold
- CLI skeleton
- migrations scaffold (empty)
- docs stubs (this file + the others in `docs/`)

### PR 2 — Evaluation governance

- fill in `docs/evaluation-plan.md`
- fill in `docs/benchmark-policy.md`
- fill in `docs/surrogate-policy.md`
- benchmark manifest schema under `data/benchmarks/`
- evaluator versioning scaffold in `src/evaluation/`

### PR 3 — Canonical schema + ingestion

- MariaDB schema migrations
- GBX.NET subprocess boundary (`src/parsers/`)
- map + replay ingestion with rate limiting, snapshot tagging, caching
- parse-status + error taxonomy
- small sample fixtures

### PR 4 — Replay cleaning

- cleaning rules: incomplete, invalid timing, teleport/spike, outlier
  speed, zero-motion, restart/reset patterns, spectator artifacts
- classification: `clean` / `usable_with_warnings` / `rejected`
- cohort separation: intent, performance, robustness
- diagnostics output

### PR 5 — Route inference scaffold

- route artifact schema
- replay projection utilities
- clustering abstraction (DBSCAN, HDBSCAN, per-segment pluggable)
- sample outputs on fixture maps

**Status: BLOCKED for TM2020 on position-telemetry route inference.**

GBX.NET 2.4.x cannot decode TM2020 ghost position/velocity samples —
`CPlugEntRecordData.EntList[i].Samples` is raw `byte[]` deltas with no
high-level decoder. The telemetry sidecar emits `samples: []` and the
replay cleaner correctly routes such replays to `telemetry_unavailable`
rejection. Full centerline inference requires real (x, y, z) samples
and is parked until one of these unlocks:

1. **OpenPlanet in-game exporter workstream** — authoritative per-tick
   motion telemetry captured at play-time via an AngelScript plugin
   and handed back through a distinct ingestion path. See
   `docs/workstreams/openplanet-telemetry.md`.
2. **GBX.NET upstream contribution** — reverse-engineer
   `CPlugEntRecordData`'s entity-record byte format and land a
   position-sample decoder on the library side. Multi-week effort;
   outcome uncertain.

What **is** unblocked today: the wrapper emits a breadcrumbs sidecar
(`.breadcrumbs.json`) alongside the telemetry sidecar, carrying the
decoded `IInput` timeline (SteerTM2020 / Accelerate / Brake / Respawn
/ ...) plus the exact `checkpoint_times_ms`. These let us build:

- per-replay driving-behavior features (input density, brake/steer
  counts, checkpoint pacing)
- breadcrumb-cohort classification (can replace cleaning rules that
  currently need samples)
- coarse race-phase segmentation via checkpoint timing

Breadcrumb-only artifacts are a different surrogate input than a full
centerline and should not be claimed as one. The PR 5 **scaffold**
(artifact schema, clustering abstraction, pipeline plumbing) can still
land on fixture/test data; scale-1k route extraction waits on the
unlock paths above.

### PR 6 — Constraint graph

- Neo4j schema
- observed adjacency extraction
- **validity evidence fields** (not frequency-as-validity)
- sample graph build on fixtures

### PR 7 — Evaluator dry-run

- run evaluator stack over benchmark + community sets
- emit `reports/evaluator-dryrun-v1.md` containing:
  - benchmark set rankings with scores
  - score distribution histograms
  - known-strong vs known-mediocre separation statistic
  - evaluator-vs-benchmark disagreements
  - cross-evaluator disagreements

## Deliverable → PR map

| Deliverable                                    | Lands in   |
|------------------------------------------------|------------|
| 1 — Evaluation plan first                      | PR 2       |
| 2 — Frozen benchmark sets                      | PR 2       |
| 3 — Canonical schema                           | PR 3       |
| 4 — Ingestion pipeline                         | PR 3       |
| 5 — Replay cleaning                            | PR 4       |
| 6 — Route inference scaffold                   | PR 5       |
| 7 — Constraint graph extraction                | PR 6       |
| 8 — Evaluator dry-run on community maps        | PR 7       |
| 9 — Operational surrogate policy stub          | PR 2       |
| 10 — CLI + developer workflow                  | all PRs    |

## After Phase 1

Out of scope for this roadmap. Model training, generator work, player-
facing product decisions happen only after the PR 7 dry-run demonstrates
that the evaluation substrate is trustworthy.
