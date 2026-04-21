# Trackmania 2020 AI Track Generator

Phase 1 of a mapper-assist system for Trackmania 2020. This repository is
currently in **bootstrap** state — no ingestion, evaluation, or generation
code has been implemented yet. The first task is to establish the
measurement and data substrate before any generator work begins.

See [`CLAUDE.md`](./CLAUDE.md) for the full operating mandate and
[`docs/roadmap.md`](./docs/roadmap.md) for the PR sequence.

## Status

| PR  | Scope                               | State       |
|-----|-------------------------------------|-------------|
| 1   | Repo bootstrap                      | done        |
| 2   | Evaluation governance               | done        |
| 3   | Canonical schema + ingestion        | done        |
| 4   | Replay cleaning                     | done        |
| 5   | Route inference scaffold            | done        |
| 6   | Constraint graph                    | done        |
| 7   | Evaluator dry-run                   | done (v1)   |

## Non-goals for Phase 1

- RL fine-tuning
- end-to-end generator training
- full UI product
- autonomous public track publishing
- support for all styles
- item / free-placement generation
- live in-game plugin integration

## Tech stack

- Python (orchestration, features, evaluation)
- MariaDB (canonical relational storage)
- Neo4j (block adjacency / transition graph)
- GBX.NET behind a subprocess/HTTP boundary for GBX parsing
- Docker Compose for local services
- CLI-first (`python -m src.cli ...`)

## Local setup

Not wired yet. Once PR 1 lands, the flow will be:

```bash
# Start local MariaDB + Neo4j
make dev-up

# Apply migrations
make migrate

# Run sample pipelines (to be implemented in later PRs)
make ingest-sample
make replay-clean-sample
make extract-route-sample
make eval-benchmark-sample
make constraints-sample

# Tests
make test
```

## Config

Two files drive configuration. Both are committed as `*.example`
templates and expected to be copied locally:

- `.env.example` → `.env` — credentials (DB passwords, TMX contact
  email). Loaded at startup by `src/utils/config.py::load_env_file`
  and merged into the process environment without overriding
  already-exported variables.
- `config/settings.example.yaml` → `config/settings.yaml` —
  non-secret configuration. Secrets are referenced via `${VAR}` or
  `${VAR:-default}` tokens that the loader substitutes from the
  environment.

```bash
cp .env.example .env
cp config/settings.example.yaml config/settings.yaml
# edit .env with your MariaDB / Neo4j credentials
```

Neither `.env` nor `config/settings.yaml` is committed (see
`.gitignore`).

## Data layout

- Code, schemas, migrations, and benchmark **manifests** live in git.
- Actual map files, replays, and derived artifacts do **not** live in git.
  They are referenced by path and content hash.
- Committed fixtures under `data/fixtures/` and `tests/fixtures/` must be
  small (kilobytes, not megabytes).

## Benchmark assets

Benchmark sets are versioned and **immutable once released**. Changing a
benchmark means publishing a new version, never editing the existing one.
See [`docs/benchmark-policy.md`](./docs/benchmark-policy.md).

## Evaluator versioning

Every evaluator carries a version. Every evaluation artifact records the
evaluator version and the benchmark-set version it was scored against. The
surrogate is treated as an operational subsystem, not a static model — see
[`docs/surrogate-policy.md`](./docs/surrogate-policy.md).

## Known limitations

- **Reference GBX wrapper ships in-tree** at `parsers/gbx-wrapper/`
  (C#/.NET 8, wraps [GBX.NET](https://github.com/BigBang1112/gbx-net)).
  Build it with `dotnet build parsers/gbx-wrapper -c Release`. The
  replay-cleaning pipeline (PR 4) consumes telemetry sidecars
  produced by this wrapper; the ghost-sample shape was scaffolded
  against the expected GBX.NET API and may need refinement once
  real TM2020 replay files are available for validation.
- TMX endpoint paths are scaffolded with placeholder URLs. Swap in
  real paths via `config/settings.yaml` before a real ingestion.
- All cleaning + cohort thresholds are initial calibration targets.
  They get re-tuned against real distributions once PR 3 ingestion
  has run at scale (see `docs/evaluation-plan.md` Milestone A).
- Constraint graph (PR 6) seeds `(:ADJACENT_TO)` from spatial
  adjacency only; directed `(:TRANSITION)` edges land when replay-
  to-block projection is wired.
- Three scaffold evaluators ship in PR 7 (`structural`,
  `adjacency_graph`, `route_coverage`); the dry-run CLI renders
  `reports/evaluator-dryrun-v1.md` from benchmark manifests + a
  community sample. Style, flow, and novelty dimensions remain
  `None` — they need trained models which are out of scope for
  Phase 1 (see `docs/evaluation-plan.md`).

## Contributing

Read `CLAUDE.md` first. PRs must stay within the milestone scope listed in
the status table above.
