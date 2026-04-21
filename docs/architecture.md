# Architecture

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

Shipped in PR 4:

- **Telemetry contract** (`src/replay/telemetry.py`). Defines the
  dataclass shape the GBX wrapper must emit. Schema version
  (`TELEMETRY_SCHEMA_VERSION`) is pinned; wrapper upgrades that
  change the payload bump it.
- **Seven cleaning rules** under `src/replay/rules/`: `incomplete`,
  `invalid_timing`, `teleport`, `outlier_speed`, `zero_motion`,
  `restart`, `spectator`. Each is a pure function of
  `ReplayTelemetry` plus a thresholds dict, returning a `RuleResult`
  with severity (`WARN` | `REJECT`) and structured evidence.
- **Classifier** (`src/replay/classify.py`): aggregates rule results
  by the policy "any REJECT → rejected; any WARN → usable_with_warnings;
  else clean". The full per-rule evidence is serialized into
  `replays.clean_diagnostics` (migration 009) so a failed replay is
  debuggable without re-running the rules.
- **Cohort assignment** (`src/replay/cohorts.py`): per-map,
  percentile-based. Finished replays at `performance_top_pct` go to
  `performance`; replays in the `[intent_lower_pct, intent_upper_pct]`
  band go to `intent`; replays in `[robustness_lower_pct,
  robustness_upper_pct]` go to `robustness`. Maps with fewer than
  `small_map_n` finished replays bypass percentiles and assign every
  cohort to every replay (there's no distribution to stratify over).
- **Pipeline + CLI** (`src/replay/pipeline.py`, `src/cli/__main__.py`):
  `python -m src.cli replay-clean` and `python -m src.cli
  assign-cohorts` emit separate `stage_run` rows and write
  `clean_status` / `clean_version` / `cohort_membership` /
  `clean_diagnostics` on the `replays` row.

All numeric thresholds are initial calibration targets documented in
`config/settings.example.yaml`; they get re-tuned once PR 3 ingestion
has populated real replay distributions (see
`docs/evaluation-plan.md` Milestone A).

### Route inference (`src/route/`)

Produces a candidate route for a map from its replay set. Clustering is
pluggable (DBSCAN, HDBSCAN, per-segment variants). Outputs include a
centerline, branch candidates, segment candidates, and diagnostics.

Shipped in PR 5 (scaffold):

- **Artifact schema** (`src/route/artifact.py`). `Centerline` is a
  sequence of `CenterlinePoint(s, x, y, z)` values with non-decreasing
  `s`. Serialized as JSON and stored on the filesystem at
  `<artifacts_root>/routes/<hash>.json`; the `route_artifacts` DB
  row references it by path + SHA-256 content hash.
- **Projection** (`src/route/projection.py`). Vectorized (numpy)
  nearest-point projection of telemetry samples onto a centerline
  polyline — foot-of-perpendicular clipped to each segment. Output
  is per-sample arc-length `s`, 3D offset vector, and segment index.
- **Clusterer abstraction** (`src/route/clusterers/`). A `Clusterer`
  ABC plus a registry (`register`, `get`, `create`). The three
  shipped clusterers — `grid` (default, numpy-only), `dbscan` (lazy
  sklearn adapter), `per_segment` (sliding-window composite) —
  demonstrate the seam. Callers never import a concrete clusterer;
  `route.create(name, params)` resolves from config.
- **Extractor** (`src/route/extract.py`). Seeds a centerline from
  the replay closest to the median total duration, projects all
  cohort replays, refines the centerline by averaging query points
  within each vertex's s-window, re-projects, then runs the
  configured clusterer on `(s, offset_x, offset_y, offset_z)` and
  emits branch candidates by cluster multiplicity per s-bin. Segment
  boundaries come from the seed replay's checkpoints, falling back
  to uniform intervals when none are present.
- **Pipeline + CLI** (`src/route/pipeline.py`,
  `src/cli/__main__.py`). `python -m src.cli extract-route` writes
  one `route_artifacts` row per map per `route_version`, with
  `clustering_method`, `clustering_params`, `replay_cohort`, and
  `extraction_confidence` recorded on the row for provenance.
  Re-runs with the same `route_version` skip existing rows; bumping
  the version allows re-extraction.

**Scaffold caveats**: the refinement, branch-detection, and
confidence-scoring heuristics are first-pass. They produce coherent
results on synthetic data but will be revisited against real replay
telemetry in later PRs. The abstractions are the load-bearing
deliverable; the heuristics are replaceable without touching them.

### Constraint graph (`src/constraints/`, `migrations/neo4j/`)

Stores observed block-to-block relations in Neo4j. Edges carry evidence
fields (observed-in-valid-maps, replay-supported count, benchmark-quality
occurrences), not just raw frequency.

Shipped in PR 6:

- **Graph schema** (`migrations/neo4j/`). `(:Block {key, family, type,
  variant})` unique on the normalized composite key;
  `(:Block)-[:ADJACENT_TO]->(:Block)` with evidence properties;
  `(:ProcessedMap {map_id, snapshot_id, parser_version})` idempotency
  ledger.
- **Extractor** (`src/constraints/extractor.py`). Emits one
  `AdjacencyObservation` per unordered axis-neighbor pair of blocks
  within a single map. Pair order is lexicographic so undirected
  adjacency collapses to a single edge.
- **Evidence policy** (`src/constraints/evidence.py`).
  `derive_validity_label` enforces the invariant: frequency is not
  validity. `benchmark_strong_count >= 1` → `valid`; `broken_fixture_count
  > 0` without any positive evidence → `suspicious`; everything else
  (including high `observed_in_maps_count`) → `unknown`.
- **Pipeline + CLI** (`src/constraints/pipeline.py`,
  `src/cli/__main__.py::build-graph`). Per-map idempotency via
  `MERGE (:ProcessedMap ...)` before any node/edge writes; single
  transaction for `UNWIND`-style batch MERGE of nodes and edges;
  validity label recomputed on every edge touch from the current
  counts (no separate label storage to drift).
- **Neo4j adapter** (`src/storage/neo4j_adapter.py`). Driver factory
  + Cypher migration runner with content-hash edit detection, mirror
  of the MariaDB runner. CLI: `python -m src.cli neo4j-migrate`.

The **directed** `:TRANSITION` edge — one inferred per clean-cohort
replay pass through a block pair — is deliberately out of scope in
PR 6. It depends on a real GBX wrapper producing connection-face
metadata and on replay-to-block projection. Neither exists yet;
building an unsupported directed edge now would invent evidence we
don't have.

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

## Evaluator versioning (resolved PR 2)

Evaluator versions use **semver** (`MAJOR.MINOR.PATCH`), not a monotonic
integer. The three-level split is load-bearing: a drift-monitoring system
needs to know whether a version bump changes scores or not, and an integer
can't carry that signal without an out-of-band table.

- **Major** — score-incompatible change. Any evaluation artifact produced
  by an older major version is treated as stale. Ranking a new version
  against an old version requires full re-scoring.
- **Minor** — additive change: new diagnostic fields, new score dimensions
  that do not alter existing ones, new optional inputs. Existing artifacts
  remain valid but incomplete.
- **Patch** — bugfix that provably does not change rankings (e.g. fixing a
  log message, tightening a type hint). Existing artifacts remain valid
  without re-scoring. Changes that might shift scores are not patch-level,
  even if the intent was "just a fix".

The version comparison rules are enforced by `src/evaluation/versioning.py`.
Evaluator classes carry `version: str` as a class attribute; every
`EvaluationArtifact` row pins the full semver string of the evaluator that
produced it.

The same convention applies to the surrogate (see `surrogate-policy.md`)
and to any future sub-evaluators (style classifier, flow surrogate,
structural validator).

## GBX.NET transport (resolved PR 3)

Chose **subprocess-per-artifact** for the PR 3 implementation. See
`src/parsers/README.md` for the wire protocol and the tradeoffs. A
long-running HTTP mode remains possible (`ParserClient` is abstract)
but is not implemented until throughput justifies it.

## Replay raw-telemetry storage (resolved PR 3)

Raw replay and map binaries are **content-addressed files on the
filesystem** under `storage.artifacts.root` (see
`src/ingestion/artifacts.py`). The DB row in `maps` / `replays` stores
the path plus SHA-256 of the bytes. Derived per-replay features live
in the DB as JSON (`replay_features.features`), because they are
small and query-shaped; raw per-tick telemetry stays on the
filesystem and will be referenced by path when the replay-cleaning
stage needs it in PR 4.
