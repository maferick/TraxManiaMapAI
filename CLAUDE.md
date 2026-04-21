# CLAUDE.md — Trackmania 2020 AI Track Generator

This file is the operating mandate for Claude Code working in this repository.
Read it before making changes.

## Project purpose

Phase 1 is a **mapper-assist** system for Trackmania 2020. It is not a
player-facing autonomous track generator. The goal of this phase is to build
the **measurement substrate and data substrate** that makes later model work
trustworthy — not to train a generator.

Success in Phase 1 means we can:

- ingest real maps and replays reliably
- represent them canonically
- clean and segment replay behavior
- freeze benchmark sets
- extract block-adjacency evidence
- evaluate existing community tracks in a way that appears sane
- do all of the above with versioned, reproducible artifacts

## Core architectural principle

**The surrogate is not a model artifact; it is an operational subsystem.**

Implications that must be honored across the codebase:

- evaluators are versioned
- benchmark sets are frozen and immutable once released
- surrogate refresh is designed as an ongoing loop
- evaluator drift is measurable
- generated tracks are never trusted on evaluator output alone without
  benchmark validation

## Non-goals for Phase 1

Do **not** implement any of the following in this phase:

- RL fine-tuning
- end-to-end generator training
- full UI product
- autonomous public track publishing
- support for all styles
- item / free-placement generation
- live in-game plugin integration

If a change drifts toward these, stop and raise it.

## What Claude Code must not do

- jump straight into transformer training
- hardwire one clustering method everywhere (e.g. DBSCAN-only)
- use only top replays for all tasks
- collapse mapper-assist and player-facing evaluation into one metric
- treat frequency as validity in the constraint graph
- define "good" only as "acceptable"
- trust evaluator outputs before benchmark dry-run validation
- commit actual map/replay binary data or large derived artifacts to git
- overwrite benchmark assets in place — changes require a new version

## Tech stack

- **Python** — orchestration, feature extraction, evaluation, ML pipeline
- **MariaDB** — canonical relational storage (maps, blocks, replays, features,
  evaluation artifacts)
- **Neo4j** — block adjacency / transition graph
- **CLI-first** — every pipeline stage is invokable from `python -m src.cli ...`
- **Docker Compose** — local dev services
- **GBX parsing** — GBX.NET isolated behind a subprocess/HTTP boundary, called
  from Python. Do not contaminate the core Python pipeline with .NET runtime
  concerns.

## Repository map

```
trackmania-ai/
  CLAUDE.md                 # this file
  README.md
  docs/
    architecture.md
    evaluation-plan.md      # required before generator work
    benchmark-policy.md     # required before generator work
    surrogate-policy.md     # governance for the surrogate subsystem
    data-contracts.md       # canonical schema contracts
    roadmap.md
  config/
    settings.example.yaml
  docker/
    docker-compose.yml
  scripts/
    bootstrap_dev.sh
    run_ingest.sh
    run_eval.sh
  src/
    cli/            # argparse/typer entry points
    ingestion/      # map + replay ingestion
    parsers/        # GBX.NET boundary wrappers
    schema/         # pydantic/dataclass models for canonical entities
    replay/         # cleaning, cohort classification, diagnostics
    route/          # centerline / route inference scaffolding
    evaluation/     # versioned evaluators
    benchmarks/     # frozen benchmark manifests + loaders
    constraints/    # adjacency/transition graph extraction
    storage/        # MariaDB + Neo4j adapters
    utils/
  tests/
    unit/
    integration/
    fixtures/
  data/
    fixtures/       # tiny committed fixtures only
    benchmarks/     # versioned manifests; raw data lives outside git
  migrations/
    mariadb/
    neo4j/
```

## Coding standards

- explicit types where feasible
- structured logging (no print)
- config-driven thresholds; no magic constants in evaluator logic
- every derived artifact is versioned
- benchmark assets are never overwritten in place
- every pipeline stage is resumable and idempotent

## Provenance requirements (load-bearing)

Every derived record carries enough lineage that any artifact can be
reproduced from its upstream inputs.

- Every row in every derived table has:
  - `created_at`
  - `created_by_version` (pipeline-stage version)
  - `source_artifact_ids` (upstream lineage)
- Every pipeline stage emits a `stage_run` record with:
  - inputs, outputs
  - resolved config hash (hash of the merged default+override config dict)
  - code version (git SHA)
  - duration
- Reproducing an artifact = look up its `stage_run`, retrieve the config and
  code version, re-run.

## Ingestion rules

- **Training subset ≠ ingestion subset.** Ingest the full available TM2020
  distribution; narrower training subsets are selected downstream.
- TMX is a community-run service. Ingestion must:
  - respect rate limits (target a conservative request/second ceiling; make it
    configurable)
  - identify the client with a clear User-Agent
  - cache aggressively so repeat runs do not re-hit the API
  - be resumable from partial failures
- Each ingestion run is tagged with a snapshot date; benchmark sets reference
  specific snapshot versions.
- TMX tag noise is expected. Phase 1 accepts self-reported tag noise; the
  style classifier (future) must be evaluated against a hand-curated
  ground-truth subset, not raw tags.

## Storage layout

- MariaDB stores canonical metadata, block placements, derived replay
  features, evaluation artifacts.
- Raw replay telemetry and large binary artifacts live on the filesystem; the
  DB stores path references plus content hashes.
- Do not put raw replay binaries or multi-megabyte fixtures in MariaDB rows.

## Re-ingestion and versioning of child records

When a map is reparsed with a new parser version:

- Every derived artifact (blocks, routes, features) has a `parse_version` /
  `source_parser_version` column and references upstream artifact versions.
- Multiple versions of derived artifacts may coexist during transitions.
- No destructive replace without an explicit migration command.

## Kill-switch for Phase 1

If after PRs 1–4 the ingestion success rate on a 10k random-map sample is
below an agreed threshold, or replay cleaning rejects more than an agreed
fraction of replays, pause before starting route inference and reassess.
Exact thresholds are defined in `docs/evaluation-plan.md`.

## First milestones (PR sequence)

1. **PR 1 — Repository bootstrap.** Structure, Docker Compose, config loader,
   CLI skeleton, migrations scaffold, docs stubs. *(this PR)*
2. **PR 2 — Evaluation governance.** `evaluation-plan.md`,
   `benchmark-policy.md`, `surrogate-policy.md`, benchmark manifest schema,
   evaluator versioning scaffold.
3. **PR 3 — Canonical schema + ingestion.** MariaDB schema, map/replay
   ingestion, parse-status + error taxonomy, sample fixtures.
4. **PR 4 — Replay cleaning.** Classification, cleaned replay artifacts,
   cohort separation, diagnostics.
5. **PR 5 — Route inference scaffold.** Route artifact schema, replay
   projection, clustering abstraction (DBSCAN/HDBSCAN/per-segment pluggable),
   sample outputs.
6. **PR 6 — Constraint graph.** Neo4j schema, observed adjacency extraction,
   validity evidence fields (not frequency-as-validity), sample graph build.
7. **PR 7 — Evaluator dry-run.** Run evaluators on benchmark/community sets,
   emit `reports/evaluator-dryrun-v1.md` with score distributions,
   known-strong vs known-mediocre separation, evaluator-vs-benchmark
   disagreements, and cross-evaluator disagreements.

Deliverable 8 (evaluator dry-run) lands in PR 7.
Deliverable 9 (surrogate policy stub) lands in PR 2.
Deliverable 10 (CLI + dev workflow) runs across all PRs.

## How to work in this repo

- Plan before coding. Large changes should begin by updating the relevant
  doc in `docs/` or opening an issue.
- Keep PRs scoped to the milestone list above. Do not bundle scope.
- Tests live next to their subsystem under `tests/`. Fixtures belong in
  `tests/fixtures/` or `data/fixtures/`, never in production code.
- When unsure whether something is Phase 1 scope, check the non-goals list
  and the milestone list. If still unclear, ask before building.
