# src/constraints

Block adjacency / transition graph. PR 6.

## Load-bearing invariant

**Frequency is NOT validity.** Rare transitions and illegal transitions
are different things. An edge that appears in 10,000 maps with no
benchmark-strong evidence is still `unknown`; an edge appearing once
in a benchmark-strong map is `valid`. See
`src/constraints/evidence.py` for the derivation policy and
`docs/evaluation-plan.md` Section B ("Adjacency graph validity") for
the full invariant list.

## Layout

| File                  | Role                                                              |
|-----------------------|-------------------------------------------------------------------|
| `nodes.py`            | `BlockKey`, `AdjacencyObservation`, `AdjacencyEdge` dataclasses.  |
| `evidence.py`         | `derive_validity_label(...)` — the "no frequency-as-validity" policy. |
| `extractor.py`        | `extract_adjacencies(placements, ...)` — pure, per-map.           |
| `pipeline.py`         | `ConstraintGraphPipeline` — DB orchestrator (MariaDB → Neo4j).    |

## Node + edge shape

- `(:Block {key, family, type, variant})` — unique by `key` (a
  normalized composite of the three identity fields; nulls become
  empty strings). Migration `001_block_node.cypher`.
- `(:Block)-[:ADJACENT_TO]->(:Block)` — lexicographically ordered
  so each undirected spatial adjacency is represented once. Carries
  evidence fields:
  - `observed_in_maps_count` — distinct maps contributing this adjacency
  - `benchmark_strong_count` — count from maps marked benchmark-strong
  - `broken_fixture_count` — count from maps marked broken-fixture
  - `replay_supported_count` — placeholder (populated by a later PR
    once replay-to-block projection lands)
  - `validity_label` — derived: `valid` / `suspicious` / `unknown`
  - `first_seen_snapshot`, `last_seen_snapshot`, `last_updated_at`
- `(:ProcessedMap {map_id, snapshot_id, parser_version})` —
  idempotency ledger. A rerun against the same key is a no-op.

## Running

Prerequisites: MariaDB migrations applied (PR 3), Neo4j running, and
block placements written (via PR 3 ingestion + a real GBX wrapper).
Then:

```bash
python -m src.cli neo4j-migrate
python -m src.cli build-graph \
    --snapshot 2026-04-tmx \
    --parser-version 0.0.0
```

Restrict to specific maps with repeated `--map-id N` flags.
Benchmark-strong / broken-fixture map ids come from
`config.constraints.{benchmark_strong_map_ids, broken_fixture_map_ids}`.

## Directed transitions — deliberately out of scope (PR 6)

A directed `:TRANSITION` edge (from block A's exit face to block B's
entry face, supported by clean-cohort replays) is the natural next
step. It depends on:

- a real GBX wrapper producing rich block metadata with connection
  faces; AND
- replay-to-block projection that identifies which blocks each
  clean-cohort replay passes through and in what order.

Both land after PR 6. The current `:ADJACENT_TO` edge captures
spatial adjacency only — a necessary substrate for the directed
edge, not a substitute.
