# Label-quality-weighted training (A4)

## Why this exists

A2 built a new label, `time_envelope_v2`, **plus** a sidecar
`label_quality_weight` per corridor in `(0, 1]` derived from the
coefficient of variation of observed inter-CP times on the corridor's
map. The intuition: maps where drivers agree on timing (low CV) give
higher-confidence labels; maps where drivers disagree give lower.

A2 deliberately kept the weight separate from the plausibility value
so the two could be consumed independently. A4 *uses* the weight —
as a **sample weight in the loss** during ridge training.

A3 established that the current learned ranker collapses diversity
by 4–12%. A4 must not make that worse; the A3 watchdog re-runs
post-A4.

## Weighted ridge regression — closed form

Standard ridge:

```
w* = argmin_w ||Xw − y||² + λ||w||²
   = (XᵀX + λI)⁻¹ Xᵀy
```

Weighted ridge (per-sample weights `s_i ≥ 0` on a diagonal `W`):

```
w* = argmin_w Σᵢ s_i (xᵢᵀw − yᵢ)² + λ||w||²
   = (XᵀWX + λI)⁻¹ XᵀWy
```

Implementation note: forming `W` as a sparse / dense diagonal matrix
is wasteful at our scale. Instead scale the rows by `√s_i`:

```
X' = diag(√s) · X        (row-scaled)
y' = √s · y
w* = (X'ᵀX' + λI)⁻¹ X'ᵀy'
```

This is algebraically identical and numerically cleaner.

## Scheme naming

A4 introduces a **fourth** label scheme alongside the existing three:

| Scheme | Labels | Weights |
|---|---|---|
| `inverse_rank` | rank proxy | uniform |
| `time_envelope` | v1 mean-based | uniform |
| `time_envelope_v2` | v2 (trimmed_mean + outlier reject) | uniform |
| **`time_envelope_v2_weighted`** | same as v2 | **A2 `label_quality_weight`** |

All four persist permanently. Comparison discipline unchanged —
same feature matrix, same train/test split (same seed), same α
sweep. The only difference between v2 and v2_weighted is the sample
weights passed to ridge.

## Anti-leakage audit

`label_quality_weight` is computed from observed replay timing
variance on each map (`stdev / mean`). Inputs:

- observed inter-CP gaps (replay telemetry)
- aggregated gap mean (same scheme-agnostic aggregator as the label
  value)

Inputs NOT used:

- `path_rank` / `is_top_rank`
- `corridor_confidence` / `learned_corridor_score`
- any ranking output
- any feature vector entry

So the weight is a property of the **map's replay data**, not the
corridor's predicted rank. A corridor on a stable-driver map gets
the same weight regardless of rank. Safe.

## Interaction with A3 watchdog

After any A4 training run, the A3 diagnostic
(`diagnose-corridor-diversity`) re-runs against the scored corpus.
If the weighted scheme collapses diversity by more than the
unweighted scheme did (A3 v0 baseline: −0.068 median, −0.020 mean),
flag in the PR body.

The weight is a **map-level** property, so we expect it to affect
between-map rankings more than within-map rankings. The A3 metric
is mostly within-map (pairwise within an interval). A3 should
therefore be roughly stable under A4 — any unexpected collapse is
worth investigating.

## What A4 does NOT ship

- **Cohort-aware weighting.** Adds leakage audit surface. Deferred.
- **Persisting the weighted model to DB.** A4 is evaluation-layer
  only. Persistence happens only after we're confident the weighted
  model wins.
- **Replacing v2 as the default.** v2 stays; v2_weighted runs
  alongside. Decision on default comes from the data.

## Success criteria

- v2_weighted improves test rank correlation or proxy-cohort AUC
  over v2 by ≥ 1% on the same feature matrix + split, OR
- v2_weighted shows no meaningful change but improves label-noise
  robustness (lower test RMSE on high-CV maps), AND
- A3 diversity watchdog is unchanged or better (not worse by > 10%).

Anything weaker than that means quality weighting isn't pulling its
weight on this corpus and we record that as a null result.

## Follow-ups (not in this PR)

- If v2_weighted clearly wins: persist it via
  `score-corridors-learned` and swap the evaluator's default.
- If null result: investigate whether the weight itself has too
  little dynamic range (stdev 0.077 on scale-1k — may be borderline
  too tight) and consider a steeper weight function like
  `1 / (1 + cv²)` or `exp(-α·cv)`.
