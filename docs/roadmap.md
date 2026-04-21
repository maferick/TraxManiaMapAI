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
