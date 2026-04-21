# Data Contracts

**Status:** stub. Concrete schema lands in PR 3 (`migrations/mariadb/`).

This file describes the canonical entities and the provenance contract.
Field lists are indicative; exact types/indexes are decided in PR 3.

## Provenance contract (applies to every derived entity)

Every derived row carries:

- `created_at` — timestamp
- `created_by_version` — version of the pipeline stage that produced it
- `source_artifact_ids` — upstream lineage (map id + parse version, replay
  id + clean version, etc.)

Every pipeline stage run records a `stage_run` row with:

- inputs + outputs
- resolved config hash (hash of the merged default+override config dict)
- code version (git SHA)
- duration

## Canonical entities

### Map

- `source_map_id`
- `source_system` (e.g. `tmx`)
- `ingestion_snapshot` (snapshot tag for reproducibility)
- `title`, `author`, `environment`
- `style_tags` (raw, self-reported — noisy)
- length metadata
- awards / rating / popularity metadata
- `has_items`, `is_block_mode`
- `parser_version`, `parse_status`
- `raw_artifact_path`, `raw_artifact_hash`

### BlockPlacement

- `map_id`
- `parser_version` (coexists across parser versions during transitions)
- block family / type, variant
- `x`, `y`, `z`, rotation
- flags, surface
- placement index
- optional raw source metadata blob

### Replay

- `source_replay_id`
- `map_id`
- player metadata (where available)
- finish time, rank metadata
- `ingestion_snapshot`
- `clean_status` (`clean` | `usable_with_warnings` | `rejected`)
- `cohort_membership` (set: intent, performance, robustness)
- `raw_artifact_path`, `raw_artifact_hash`

### ReplayFeatures (derived)

- `replay_id`, `feature_extractor_version`
- normalized derived features
- diagnostics

Raw telemetry samples should not live in MariaDB rows. They live on the
filesystem, referenced by path + hash. Derived features are the DB-resident
representation.

### RouteArtifact

- `map_id`, `route_version`
- centerline artifact (path reference)
- branch definitions
- segment boundaries
- extraction provenance (clustering method, parameters, replay cohort used)
- extraction confidence + diagnostics

### EvaluationArtifact

- `map_id`
- `evaluator_version`
- `benchmark_set_version`
- structural score
- drivability score
- flow score
- style score
- novelty score
- diversity metadata
- notes / diagnostics

## Size / volume expectations (rough)

- ~267k maps × ~50 KB parsed ≈ ~13 GB for maps alone
- replays are larger — hundreds of KB per replay, GB-scale per popular map

Implication: **raw replay and map blobs stay on the filesystem**, not in
MariaDB rows. Confirm exact storage layout in PR 3.

## Idempotency for child records

When a map is reparsed with a new parser version:

- `BlockPlacement` rows are **added** under the new `parser_version`, not
  overwritten.
- Dependent artifacts (routes, features) reference the `parser_version`
  they were produced from.
- Destructive replacement requires an explicit migration command and is
  never the default.
