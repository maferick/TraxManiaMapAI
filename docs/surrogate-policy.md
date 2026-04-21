# Surrogate Policy

## Principle

> The surrogate is not a model artifact; it is an operational subsystem.

The flow / drivability surrogate (and any future surrogates over true
simulation) is governed as a living subsystem with versioning,
retraining triggers, drift monitoring, and explicit guardrails against
surrogate-only trust. This document is the governance contract. The
evaluation thresholds that gate acceptability live in
`docs/evaluation-plan.md`; this document covers how the surrogate
*stays* acceptable.

## Versioning

- Surrogate versions use **semver** (`MAJOR.MINOR.PATCH`), prefixed
  with the surrogate name. Example: `flow-surrogate-v1.3.0`.
- Semantics mirror evaluator versioning (see `architecture.md`):
  - **major** — score-incompatible. Generator training runs that
    targeted an older major version are not comparable to runs
    targeting the new one without a re-evaluation.
  - **minor** — additive. New diagnostic outputs, new input fields
    treated as optional.
  - **patch** — no change to rankings. Bugfixes, logging, perf.
- Every evaluation artifact pins the exact surrogate version it used.
- Old surrogate versions remain runnable for reproducing past
  evaluations. A surrogate release is never "retired" by deletion; it
  is retired by being superseded in the active-version pointer, and
  the bytes stay available.

## Retraining triggers

Retraining is initiated when **any** of the following is true.
Thresholds below are initial calibration targets; they get revisited
after the first full ingestion pass (end of PR 4). Moving a threshold
is a doc change and must land in git.

### Trigger 1 — drift against the true simulator

On a fixed holdout of community maps:

- **Minor-version bump required**: Spearman ρ between surrogate
  ranking and true-simulator ranking drops below **0.80**.
- **Major-version bump required**: ρ drops below **0.70**, OR MAE on
  per-segment velocity exceeds **1.5×** the baseline value recorded
  at the previous major release.

A minor bump from this trigger does not invalidate prior artifacts
(they remain valid by semver rules); a major bump does.

### Trigger 2 — benchmark set version change

When a benchmark set version lands that meaningfully extends the
evaluation population (e.g. adds a new category of maps the surrogate
has never scored), retrain is **optional but documented**. The
surrogate need not retrain for every benchmark version — only for
ones that widen the distribution the surrogate is claimed to cover.

The decision to retrain or not is recorded in the benchmark's release
notes (`data/benchmarks/CHANGELOG.md`, introduced with the first
real benchmark).

### Trigger 3 — generator-distribution shift

When the generated-track distribution moves substantially from the
distribution the surrogate was last trained on. Measured on the
**recent generated-track holdout**, not on community maps:

- **Minor-version bump**: ρ on the generated-track holdout falls below
  **0.60** (matches the floor in `docs/evaluation-plan.md`).
- **Major-version bump**: ρ on the generated-track holdout falls
  below **0.45**.

This trigger is the critical one. A surrogate trained only on
community maps will miss failure modes specific to generator output
— exactly the regime where trustworthy scores matter most. Trigger 3
catches that drift early.

## Recalibration loop

The surrogate is refreshed against the **recent generated-track
distribution**, not only historical community maps. Concretely:

1. Each generator evaluation batch contributes its generated tracks
   (plus true-simulator results on a sampled subset) to a rolling
   training-candidate pool.
2. The pool has a sliding horizon — older batches age out as newer
   ones accumulate. Horizon length is a surrogate-training config,
   not a constant in code.
3. A retrain run mixes community-map data with generated-map data.
   The mix ratio is versioned as part of the surrogate release.
4. Retrain never runs on community-map data alone once the rolling
   pool has non-trivial size. A community-only surrogate is only
   acceptable at the very first release, and only with a loud flag
   that it will be superseded.

## Drift monitoring

Cadence: **per-release**, not per-run. Running full drift metrics on
every evaluation run is a waste of compute — drift is a trend, not a
per-request quantity.

Per release (of the surrogate or of a benchmark), record:

- surrogate-vs-simulator ρ on the fixed community holdout
- surrogate-vs-simulator ρ on the fixed generated holdout
- MAE on per-segment velocity, both holdouts
- score distribution shift on a reference set (fixed 100-map set
  pinned at the first stable release), measured as KL divergence of
  the score histogram

Drift metrics are published next to evaluation artifacts. A drift
dashboard is not Phase 1 scope; the raw numbers being present and
comparable across versions is.

## Guardrails

1. A track that scores well only on the surrogate but fails benchmark
   dry-run validation is **not accepted**. The surrogate is a filter,
   not a final grader. This is load-bearing: generators that optimize
   against a cheap surrogate will find surrogate failure modes before
   they find real-quality failure modes, and benchmark dry-run is the
   backstop.
2. Surrogate-only ranking of candidate generated tracks is permitted
   for cheap **filtering** (top-K candidate selection before true-sim
   scoring). It is not permitted for final judgement or for any
   evaluation artifact that enters a report.
3. Generator training that optimizes against the surrogate must
   include regular true-simulator checkpoints. Without them, the
   generator will overfit the surrogate — the failure mode is well
   understood and we do not need to rediscover it.
4. A surrogate major-version bump does not silently invalidate
   generator checkpoints produced against the old major. Those
   checkpoints remain reproducible but are flagged in the evaluator's
   artifact rows as "scored against superseded surrogate major".

## Release pipeline

Each surrogate release goes through:

1. **Train** against the current training-candidate pool (mix ratio
   versioned).
2. **Score** on the fixed holdouts and record all drift metrics.
3. **Compare** drift metrics to the previous release of the same
   surrogate.
4. **Classify** the bump as major / minor / patch using the rules in
   this document. Misclassification is a hard error: a score-shifting
   change cannot be shipped as a patch.
5. **Merge** the release record (metric dump + version + mix config
   hash) into the surrogate release log. The model weights themselves
   live in artifact storage, not in git.
6. **Announce** in `data/benchmarks/CHANGELOG.md` if the bump is
   major (because a major bump invalidates stored artifacts and
   anyone reading prior eval reports needs to know).

Steps 4 and 5 are both gates: a release that cannot be classified or
logged does not ship.

## Benchmark refresh vs surrogate refresh

Benchmark refresh is a separate process. A surrogate retraining does
not imply a benchmark change. Benchmark changes are explicit and
versioned under `docs/benchmark-policy.md`. Conversely, a benchmark
version change may or may not trigger surrogate retrain (see Trigger
2 above) — the decision is documented per benchmark release.

## Out of scope for Phase 1

- Any concrete surrogate training code. No surrogate exists yet;
  this document governs the one that will.
- Automated drift dashboards.
- Cross-surrogate comparisons (we have zero surrogates, so one-of-N
  reasoning is not yet relevant).
