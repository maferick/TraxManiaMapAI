# migrations/mariadb

SQL migrations for the canonical relational store. Empty in PR 1. The
first real migrations land in PR 3 and should define the canonical
entities described in `docs/data-contracts.md`:

- `map`
- `block_placement`
- `replay`
- `replay_features`
- `route_artifact`
- `evaluation_artifact`
- `stage_run` (provenance)
