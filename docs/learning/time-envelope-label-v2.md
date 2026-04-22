# Time-envelope label refinement (A2 — v2)

## Background

v1 time-envelope label (`src/corridor/ranking/time_envelope_labels.py`)
scores a corridor on whether its length fits the observed elapsed
time between checkpoints on that map. Per-map aggregation uses the
**mean** of all clean replays' inter-CP gaps.

Phase 4 v0.4 diagnostic + post-ingest analysis produced these
findings that A2 responds to:

- Label stdev under v1 was 0.103 → 0.155 after deeper ingest.
- Prediction stdev tracked labels (ceiling-bound).
- *Feature ablation* revealed path_length_log went from top variance
  carrier to dead weight once replay coverage widened — meaning the
  evidence features started to matter. But the label itself is still
  map-uniform: every corridor on a map shares one observed mean.

A2's scope is to refine the label *without* adding circularity
(no rank-derived inputs, no learned-score features feeding back).

## Hard constraint: this corpus is plain-CP-only

| scale-1k corpus | count |
|---|---|
| Maps with plain CPs (waypoint_order = 0) | 620 |
| Maps with true Linked-CPs (waypoint_order > 0) | **1** |
| Corridor-owning maps with only 1 interval (Spawn→Goal) | 163 / 206 (79%) |
| Corridor-owning maps with 2 intervals | 42 / 206 (20%) |

**Per-segment time-envelope labels are a dead end on this corpus.**
The original A2 sketch ("per-segment labels on Linked-CP maps") would
cover 1 map out of 620. Not worth the build.

What *is* tractable: **variance-aware + robust aggregation** on the
map-mean path we already have. Same per-corridor label dimension,
but a richer value and a confidence signal.

## Scope — what v2 ships

### 1. Aggregation methods

Replace the hard-coded `statistics.mean` on inter-CP gaps with a
pluggable aggregator:

- **`mean`** (v1 default, kept)
- **`median`** — robust to outlier runs (crashed attempts, brief
  physics glitches)
- **`trimmed_mean(q)`** — drop top and bottom `q` fraction, mean
  the middle. Default `q = 0.1`.

Pick per-label-build call; surface via `aggregation_method` in the
provenance dict. No auto-tuning — user chooses explicitly.

### 2. Outlier rejection (pre-aggregation)

Before aggregating, optionally drop replay gaps that are > k
standard deviations from the initial sample mean. Default `k = 3.0`,
can be disabled by setting `k = None`. Matches the replay-cleaning
rule vocabulary (large-gap rejection) but applied at label time.

### 3. Variance-aware label value

Current plausibility formula:

```
expected_time_ms = length × block_size / speed × 1000
rel_err = |observed - expected| / observed
plausibility = exp(-rel_err)
```

v2 augments this with the **coefficient of variation** (CV) of
observed inter-CP times across replays on that map:

```
cv = aggregated_stdev / aggregated_mean
label_confidence_weight = 1 / (1 + cv)
plausibility_v2 = plausibility_v1        (same value)
label_quality = label_confidence_weight  (new sidecar)
```

Two outputs per corridor: (1) the plausibility label (same unit as
v1, comparable), (2) a `label_quality` weight in `(0, 1]`. Maps
where drivers converge on a stable time → high quality; maps where
they disagree wildly → low quality. Training can use the weight as
a sample-weight in the loss.

Deliberate non-choice: **do not bake quality into the plausibility
value itself** (that would conflate "length plausible" with "label
trustworthy"). Keep them separate so downstream can choose how to
combine.

### 4. Provenance

Every v2 label dictionary is accompanied by a metadata dict:

```python
{
    "label_scheme": "time_envelope_v2",
    "scheme_version": "0.2.0",
    "aggregation_method": "trimmed_mean",
    "trimmed_q": 0.1,
    "outlier_rejection_sigma": 3.0,
    "speed_prior_m_s": 30.0,
    "block_size_m": 32.0,
    "replay_count_per_map": {map_id: n, ...},
    "generated_at": ISO8601,
}
```

Persisted alongside the model JSON so a reader can reproduce the
exact labels.

## Anti-leakage guardrails (recorded inline in the module)

Reviewer audit checklist:

- [ ] No `path_rank` / `is_top_rank` used as label input.
- [ ] No `corridor_confidence` / `learned_corridor_score` fed into
      the label computation.
- [ ] No cohort_membership influencing label **value** (cohort-aware
      aggregation is a follow-up; out of A2 scope explicitly).
- [ ] Label uses only: observed replay times, path length, speed
      prior, block size.
- [ ] `label_quality` uses only observed replay timing variance, no
      learned outputs.

## What v2 does NOT ship

- Per-segment labels (dead end on this corpus, documented above).
- Cohort-aware aggregation. Adds leakage audit surface; if we pursue
  later, audit deserves its own PR.
- Per-block-family speed priors. Nice but out of A2 scope; deferred.
- Label replacement. v1 stays alongside v2 permanently so comparisons
  remain reproducible.

## Success condition

- v2 labels widen the useful signal (label stdev or label_quality
  distribution informative) without introducing leakage.
- `diagnose-corridor-ranking` reports all three schemes side-by-side
  (`inverse_rank`, `time_envelope`, `time_envelope_v2`) — decision
  data for whether to promote v2 to default.

## Follow-ups (not in this PR)

- If the `label_quality` weight noticeably improves test rank corr
  when used as a sample-weight in training, productionize it.
- If v2 flattens, the next lever is either (a) cohort-aware
  aggregation with a careful audit, or (b) wait for OpenPlanet
  telemetry and skip this ceiling entirely.
