# migrations/neo4j

Ordered, forward-only Cypher migrations for the constraint graph.

## Naming

`NNN_short_name.cypher`. Files are applied in lexicographic order.
Applied migrations are tracked via `_Migration` nodes (one per file)
created by `000_bootstrap.cypher` — editing an applied file is a
bug; the runner recomputes the content SHA-256 and refuses the run.

## Rules

- **Forward-only.** No down-migrations.
- **Edit-once.** After a migration has been applied anywhere, editing
  its contents is a bug. Content hash catches it.
- **Additive first.** Prefer new constraints/indexes over altering
  existing ones. Destructive changes must be called out at the top
  of the file.

## Applying migrations

```bash
python -m src.cli neo4j-migrate
```

Reads connection details from `config/settings.yaml` under
`storage.neo4j`. Idempotent — re-running does nothing if everything
is already applied.

## Schema shape (PR 6)

- `(:Block {key, family, type, variant, ...})` — unique by `key`
  (a normalized composite of family/type/variant).
- `(:_Migration {filename, content_sha256, applied_at})` — internal
  tracking.
- `(:Block)-[:ADJACENT_TO {observed_in_maps_count, benchmark_strong_count,
  broken_fixture_count, replay_supported_count, validity_label,
  first_seen_snapshot, last_seen_snapshot, last_updated_at}]->(:Block)`.

Future evidence edges (`:TRANSITION` for directional replay-supported
transitions) land when replay-block projection is implemented.
