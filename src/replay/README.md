# src/replay

Replay cleaning + cohort assignment. PR 4.

## Layout

| File / dir        | Role                                                              |
|-------------------|-------------------------------------------------------------------|
| `telemetry.py`    | `ReplayTelemetry` + `SampleFrame` dataclasses — wrapper contract. |
| `rules/`          | Seven cleaning rules, one per module; `default_rules()` registry. |
| `rules/base.py`   | `Rule` ABC, `RuleResult`, `Severity`, `run_rules(...)`.           |
| `classify.py`     | Aggregate `RuleResult`s into a `CleanStatus`.                     |
| `cohorts.py`      | Per-map percentile-based cohort assignment.                       |
| `pipeline.py`     | DB orchestrators: `ReplayCleanPipeline`, `CohortAssignmentPipeline`. |

## The telemetry contract

The GBX wrapper (external; not yet built) is expected to emit a
`<raw_artifact_path>.telemetry.json` sidecar for each replay. The
`FileTelemetryLoader` reads and validates this JSON via
`telemetry.from_dict()`. Tests inject `_DictTelemetryLoader` to bypass
the filesystem.

The contract is a frozen schema: any change requires bumping
`TELEMETRY_SCHEMA_VERSION`. Sample fields — `time_ms`, `x/y/z`,
`vx/vy/vz` — are documented in `telemetry.py`.

## Running

After `python -m src.cli migrate` (which applies migration 009 adding
`replays.clean_diagnostics`):

```bash
# Classify unprocessed replays; writes clean_status + diagnostics.
python -m src.cli replay-clean --snapshot 2026-04-tmx --limit 5000

# After cleaning has run, compute cohort memberships per map.
python -m src.cli assign-cohorts --snapshot 2026-04-tmx
```

The two commands emit separate `stage_run` rows and can be rerun
idempotently — `replay-clean` reprocesses only replays still in the
`unprocessed` state; `assign-cohorts` recomputes cohorts for every
eligible replay under the given snapshot.

## Classifications and cohorts

Classifications:

- `clean` — all rules passed
- `usable_with_warnings` — at least one rule raised `WARN` but none rejected
- `rejected` — at least one rule raised `REJECT`

A replay may belong to one or more cohorts; cohorts are not collapsed
into a single filtered set.

- **intent / route-inference** — broad, cleaned, median-player runs
- **performance / optimization** — stronger / top runs
- **robustness** — wider replay distribution

## Thresholds are calibration targets

Every numeric threshold in `config/settings.yaml::replay_cleaning`
and `config/settings.yaml::cohorts` is an initial value. Re-tuning is
a `docs/evaluation-plan.md` Milestone A activity, not a code change
— update the YAML and the `stage_version` bump reflects the change
in every downstream `clean_version` record.

## Not in scope for PR 4

- the GBX wrapper itself
- a replay viewer / visualization of diagnostics
- per-checkpoint feature extraction (that's `src/replay/features/`,
  which lands alongside route inference in PR 5 as needed)
