# Surrogate Policy

**Status:** stub. Full content lands in PR 2.

## Principle

> The surrogate is not a model artifact; it is an operational subsystem.

The flow / drivability surrogate (and any future surrogates over true
simulation) must be governed as a living subsystem with versioning,
retraining triggers, drift monitoring, and explicit guardrails against
surrogate-only trust.

## Versioning rules

- Each surrogate release has a version id, e.g. `flow-surrogate-v1.3.0`.
- Every evaluation artifact records the exact surrogate version used.
- Old surrogate versions remain runnable for reproducing past evaluations.

## Retraining triggers

Retraining is initiated when any of the following occurs (exact thresholds
to be set in PR 2):

- drift between surrogate and true-simulator on a holdout set exceeds
  threshold
- new benchmark set version lands
- the generated-track distribution moves substantially from what the
  surrogate was last trained on

## Recalibration loop

The surrogate must be refreshed against the **recent generated-track
distribution**, not only historical community maps. This matters: a
surrogate trained only on community maps will miss failure modes specific
to generator output.

## Drift monitoring

Continuously (per-release cadence, not per-run) track:

- surrogate-vs-simulator error on a fixed holdout
- surrogate-vs-benchmark disagreement rate
- score distribution shift on a fixed reference set

Drift metrics are published next to evaluation artifacts.

## Guardrails

- A track that scores well only on the surrogate but fails benchmark
  validation is **not accepted**.
- Surrogate-only ranking of candidate generated tracks is permissible for
  cheap filtering; it is not permissible for final judgement.
- Generator training that optimizes against the surrogate must include
  regular true-simulator checkpoints; without them, the generator will
  overfit the surrogate.

## Benchmark refresh

Benchmark refresh is a separate process from surrogate refresh. A
surrogate retraining does not imply a benchmark change; benchmark changes
are explicit and versioned (see `benchmark-policy.md`).
