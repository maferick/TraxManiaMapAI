# Data Contracts

This file describes the canonical entities and the provenance contract.
The shipped schema lives in `migrations/mariadb/` (PR 3). Field lists
here track the shipped columns; adding a column is a new migration plus
a doc update.

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
- `clean_status` (`unprocessed` | `clean` | `usable_with_warnings` | `rejected`)
- `clean_version` (semver of the cleaning stage; pinned per row)
- `cohort_membership` (JSON array with any of: `intent`, `performance`, `robustness`)
- `clean_diagnostics` (JSON: per-rule evidence from PR 4's rule stack)
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
- `centerline_path` — filesystem location under `storage.artifacts.root/routes/<hash>/<hash>.json`
- `centerline_hash` — SHA-256 of the canonical JSON body
- `branches` — JSON list of `BranchCandidate` entries
- `segment_boundaries` — JSON list of `SegmentBoundary` entries
- `clustering_method` + `clustering_params` — pluggable clusterer provenance
- `replay_cohort` — which cohort's replays fed the extraction (typically `intent`)
- `extraction_confidence` — DECIMAL(5,4), from the extractor's confidence heuristic
- `diagnostics` — JSON blob of per-run stats (seed replay id, sample count, cluster count, …)
- `created_by_version` — extractor stage version
- `source_artifact_ids` — mapping of upstream replay ids → their content hashes

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
