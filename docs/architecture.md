# Architecture

Stub. To be filled in during PR 1–2.

## Intent

This document describes the high-level architecture of the Phase 1 system:
ingestion, canonical storage, replay cleaning, route inference scaffolding,
adjacency graph, and evaluation. It is not an implementation guide — it
describes the boundaries between subsystems and the contracts between them.

## Subsystems

### Ingestion (`src/ingestion/`)

Pulls maps and replays from upstream sources (TMX first). Responsible for:

- rate-limited, polite HTTP fetch with caching
- snapshot tagging (each run records the snapshot date)
- resumable / idempotent batch processing
- emitting raw artifact files plus a canonical metadata row

Out of scope: parsing GBX content. Ingestion hands raw bytes off to the
parser boundary.

### Parser boundary (`src/parsers/`)

GBX parsing runs in a separate process (GBX.NET), called from Python via a
subprocess/HTTP boundary. The Python layer sees only parsed structured
output. This keeps the .NET runtime isolated from the main pipeline.

### Canonical schema (`src/schema/`, `migrations/mariadb/`)

Defines the canonical entities: Map, BlockPlacement, Replay, ReplayFeatures,
RouteArtifact, EvaluationArtifact. See `docs/data-contracts.md` for the
full contract. Every derived entity carries provenance fields.

### Replay processing (`src/replay/`)

Cleans and classifies replays into `clean` / `usable_with_warnings` /
`rejected`. Assigns each replay to one or more cohorts:

- **intent / route-inference cohort** — broad, median-player runs
- **performance cohort** — stronger / top runs
- **robustness cohort** — wider distribution

Cohorts are tracked per-replay; a replay may belong to multiple cohorts.

### Route inference (`src/route/`)

Produces a candidate route for a map from its replay set. Clustering is
pluggable (DBSCAN, HDBSCAN, per-segment variants). Outputs include a
centerline, branch candidates, segment candidates, and diagnostics.

### Constraint graph (`src/constraints/`, `migrations/neo4j/`)

Stores observed block-to-block transitions in Neo4j. Edges carry evidence
fields (observed-in-valid-maps, replay-supported count, benchmark-quality
occurrences), not just raw frequency.

### Evaluation (`src/evaluation/`, `src/benchmarks/`)

Versioned evaluators, frozen benchmark manifests, and a dry-run path that
scores existing community maps. The evaluator is a first-class operational
subsystem, not a static model artifact.

### Storage (`src/storage/`)

Adapters for MariaDB and Neo4j. All large binary artifacts live on the
filesystem; the DB stores path + content-hash references.

## Cross-cutting

- **Provenance** — see `CLAUDE.md`. Every derived row carries lineage.
- **Config** — see `config/settings.example.yaml`. All thresholds come
  from config, not code constants.
- **Versioning** — every artifact type has a version column and upstream
  version references.

## Open design questions

- Exact GBX.NET transport: subprocess stdio vs. a long-running local HTTP
  service. Decide in PR 3.
- Replay raw-telemetry storage format. Decide in PR 3 or PR 4.
- Evaluator versioning: semver vs. monotonic integer. Decide in PR 2.
