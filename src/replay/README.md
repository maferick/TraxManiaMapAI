# src/replay

Replay cleaning + cohort classification. Not implemented yet. Lands in PR 4.

## Cohorts

A replay may belong to one or more cohorts. Cohorts must not be collapsed
into a single filtered set.

- **intent / route-inference** — broad, cleaned, median-player runs
- **performance / optimization** — stronger / top runs
- **robustness** — wider replay distribution

## Classifications

- `clean`
- `usable_with_warnings`
- `rejected`
