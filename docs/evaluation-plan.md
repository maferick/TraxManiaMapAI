# Evaluation Plan

This document defines the evaluation contract for Phase 1. Evaluation is
not an afterthought and is not derived from generator output — it is the
substrate the generator will eventually be measured against.

All numeric thresholds below are **initial calibration targets**. Real
distributions are unknown until PRs 3–4 land a full ingestion and
cleaning pass. After that pass, thresholds in Sections B and E get
revisited in place (a threshold change is a doc update, not a code
change, and must be recorded in git history).

## A. Product tracks

Two distinct product tracks exist. They must not be collapsed into a
single success metric. The two tracks evaluate different things and
reward different failures.

### Mapper-assist (Phase 1)

Success means:

- route skeletons are **interpretable** — a human mapper can read them
- outputs are **editable** — a human mapper can change them without
  rebuilding the whole track
- outputs are **useful starting points** — measured by panel rating,
  not by finish time or leaderboard placement

An autonomous-playable score is **not** a mapper-assist success metric.

### Player-facing (later phase, not Phase 1)

Defined here only to prevent architectural drift. Player-facing success
requires both an acceptability threshold and a competitiveness threshold
against community maps. Phase 1 does not ship player-facing autonomy
and does not tune any of its subsystems against a player-facing target.

## B. Subsystem evaluation

Each subsystem below has its own benchmark strategy and pass/fail
threshold. Subsystem scores do not aggregate into a single "evaluator
score" — aggregating them hides exactly the kind of failure we care
about (e.g. strong flow score masking a broken structural validator).

### Style classifier

- **Benchmark**: a hand-curated labeled set of ~500 maps spread across
  the Phase 1 seed styles (tech, dirt, fullspeed, RPG, LOL, mini-RPG).
  TMX self-reported tags are **not** used for ground truth — see
  `CLAUDE.md`.
- **Primary metric**: macro-F1 across styles on a held-out split.
- **Secondary**: confusion matrix, reported per-pair. Adjacent-style
  confusion (tech ↔ dirt, fullspeed ↔ LOL) is scrutinized separately.
- **Initial threshold**: macro-F1 ≥ 0.70.
- **Fail behavior**: below threshold means no style-conditioned
  generation. The surrogate and reward model may still be trained, but
  style is treated as unavailable metadata.

### Reward model

- **Benchmark**: pairwise-preference sets from the core human panel
  (see Section C). Minimum 300 labeled pairs per style before the
  reward model is considered viable for that style.
- **Primary metric**: agreement-with-majority on held-out pairs.
- **Secondary**: calibration — does the model's scalar score correlate
  monotonically with panel ordering?
- **Initial threshold**: ≥ 70% agreement AND inter-rater α ≥ 0.50 on
  the underlying label set. If α is below 0.50, the target is
  ill-defined and the agreement number is not trustworthy.

### Flow / drivability surrogate

Governed by `docs/surrogate-policy.md`. Evaluation contract here:

- **Benchmark**: fixed holdout of community maps with true-simulator
  runs, plus a separate holdout of generated maps (populated over time;
  see surrogate policy).
- **Primary metrics**: MAE on per-segment velocity; Spearman ρ on
  finish-time ranking within the holdout.
- **Initial thresholds**: ρ ≥ 0.80 on community holdout; ρ ≥ 0.60 on
  generated holdout. The generated threshold is lower on purpose — if
  the surrogate is trained only on community maps, generator-specific
  failure modes are under-represented in its training distribution,
  and a ρ-gap is the signal that triggers surrogate refresh.
- **Guardrail**: the surrogate is never the final judge. A track that
  scores well only on the surrogate but fails benchmark dry-run is
  **not accepted**. See surrogate policy.

### Structural validator

- **Benchmark**: a hand-curated split of (a) structurally broken
  fixtures — unreachable blocks, impossible transitions, no-finish
  graphs — and (b) structurally valid community maps drawn from the
  benchmark sets.
- **Primary metrics**:
  - true-positive rate on the broken set (catch actual breakage)
  - false-positive rate on the valid set (don't nuke good maps)
- **Initial thresholds**: TPR ≥ 0.95 on broken, FPR ≤ 0.02 on valid.
  FPR is the tighter constraint — a validator that flags 5% of real
  maps as broken poisons every downstream pipeline that consumes it.

### Adjacency graph validity

The adjacency graph is not scored by a single metric; it is gated by
**invariants**. Any invariant failure is a blocker.

1. **No frequency-as-validity.** An edge must carry at least one
   evidence field beyond raw observed count (e.g. appears in a
   benchmark-strong map, replay-supported in a clean-cohort replay).
2. **Benchmark-strong edges are never flagged invalid.** If a
   transition shows up in a benchmark-labeled strong map, it is valid
   by construction. This is the sanity floor.
3. **Broken-fixture-only edges are not auto-promoted.** Edges that
   only appear in structurally broken fixtures require explicit
   manual review before being labeled valid.
4. **Evidence fields are per-snapshot.** Re-ingesting a newer snapshot
   does not silently overwrite prior evidence; new evidence is added,
   old evidence is retained with its snapshot tag.

See `constraints/` design in PR 6 for the concrete evidence schema.

### Route inference quality

- **Benchmark**: fixture maps that ship with a hand-authored
  ground-truth centerline and (where relevant) known alternate lines.
- **Primary metrics**:
  - centerline coverage — fraction of clean-cohort replay points
    falling within ε of the inferred centerline
  - branch recall — fraction of known alternate lines that appear in
    the inference output
- **Initial thresholds**: coverage ≥ 0.85 on strong-tech fixtures;
  branch recall ≥ 0.60 on alternate-line fixtures. Alternate-line
  recall is deliberately softer because clustering behavior on
  alternates depends heavily on replay-cohort choice — the clustering
  abstraction (see PR 5) must let us change this choice without
  changing the rest of the pipeline.

## C. Human evaluation protocol

Human evaluation is load-bearing and cannot be replaced by any
automated surrogate. The protocol has four pieces.

### Two-tier reviewer model

- **Core panel**: 5–7 experienced mappers. Reviews every generated
  batch and every new benchmark version. Small enough to stay
  engaged; large enough to detect single-reviewer bias. Rotation is
  allowed but never >50% turnover at once (continuity is the point).
- **Expert pool**: 15–25 occasional reviewers. Drawn for specific
  decisions: disputing a kill-switch, validating a proposed new
  benchmark category, sanity-checking a surrogate major-version bump.
  Not used for routine batch rating.

### Pairwise baseline policy

Generated tracks are compared against a **matched baseline** drawn
from the community-maps ingestion snapshot — filtered to the same
style + length bucket as the generated track. Baselines are stratified
so that we know whether a generator is beating median community maps
or beating bad community maps.

Generated tracks are never evaluated only against other generated
tracks. That produces self-referential ratings and hides mode
collapse.

### Inter-rater agreement

Mandatory. Reported per rating dimension on the core panel using
Krippendorff's α. Per-rating-dimension is the key detail — an overall
α is meaningless if it mixes a well-defined dimension (finishability)
with an ill-defined one (fun).

- α ≥ 0.60 — usable
- 0.40 ≤ α < 0.60 — usable with caveats; document them
- α < 0.40 — the rating dimension is under-specified, not the raters.
  Investigate and refine before trusting any score that depends on it.

### Stated vs behavioral preference

These are tracked as separate quantities and never averaged.

- **Stated preference**: the core panel's explicit ratings.
- **Behavioral preference**: time spent on a track, replay count,
  finish rate on community servers (if obtainable).

Phase 1 is mapper-assist, so stated preference is primary. For the
later player-facing phase, behavioral preference becomes primary. Note
this is a deliberate divergence from mapper opinion: players and
mappers value different things, and the behavioral signal captures
what players do, not what mappers say they should like.

## D. Diversity evaluation

Diversity is a mandatory gate on any generator batch. A generator
that produces high-quality but near-identical outputs has failed.

### Pairwise similarity distribution

For every generated batch, compute pairwise cosine similarity in the
block-histogram + route-feature space. Report the full distribution,
not just the mean.

- **Red flag**: median pairwise similarity > 0.85 within a batch.
  This is mode collapse.

### Feature-space coverage vs training corpus

Per feature dimension, compute KL divergence between the
generated-batch marginal and the training-set marginal.

- **Red flag**: any dimension with KL > 2.0 (either direction —
  collapse or drift). A generator concentrated on a narrow region of
  feature space has mode-collapsed; one producing values never seen
  in training has drifted.

### Repeated motif detection

Run the adjacency-graph motif finder (PR 6 artifact) on the generated
batch.

- **Red flag**: any motif that appears in > 30% of outputs. Motifs
  are the scale at which a human notices repetition — repeated
  single blocks are not the concern.

### Novelty floor

Per generated track, compute nearest-neighbor distance to the
training-corpus track set in the same feature space.

- **Red flag**: distance < 0.30. Tracks below this floor are
  near-duplicates of training data and are not accepted as novel
  generator outputs.

All four diversity checks must exist and run before any generator
work is trusted — diversity is not a retrospective metric.

## E. Exit criteria

Milestone thresholds gate the next phase. Crossing Milestone A
unlocks route inference; crossing Milestone B unlocks generator work;
Milestone C governs player-facing work in a later phase.

### Milestone A — data / evaluator viability

- Ingestion success rate **≥ 80%** on a 10k random-map sample.
- Replay cleaning: not-rejected fraction **≥ 60%** on the same
  sample (i.e. the sum of `clean` + `usable_with_warnings` must be
  at least 60% of total).
- Benchmark reproducibility: rebuilding a benchmark manifest from
  its pinned snapshot yields the same `{map_id, content_hash}` set,
  **100%** match.

### Milestone B — mapper-assist viability

- Core panel rates **≥ 60%** of generated route skeletons as
  "useful starting point" or better, averaged over 3 batches.
- Structural validator + route sanity check pass rate **≥ 85%** on
  generated skeletons.

### Milestone C — competitive quality (later phase, not Phase 1)

Both thresholds required:

- Acceptability **≥ 80%** (panel rates the track as shippable).
- Competitiveness **≥ 50%** pairwise win-rate against matched
  community baseline.

"Acceptable" alone is not sufficient as final success — a merely
acceptable track is not a goal, it is a floor.

## Kill-switch for Phase 1

If after PRs 1–4:

- ingestion success rate on a 10k random-map sample falls below
  **80%** (Milestone A floor), **or**
- replay cleaning not-rejected fraction falls below **60%**,

then pause before starting route inference (PR 5). The failure mode
we are avoiding: building route inference and the constraint graph on
a biased sample, discovering the bias only in PR 7's dry-run, and
having to redo the entire downstream stack.

Invoking the kill-switch is not a failure — it is the mechanism
working. On invocation:

1. Diagnose which threshold was missed and why.
2. Decide whether the fix is at the ingestion layer (parser, rate
   limits, source coverage) or the cleaning layer (rules are too
   aggressive, cohort definitions are wrong).
3. Resume PR 5 only after re-running the 10k sample and crossing
   both thresholds.

## Evaluator versioning

Every evaluator class carries a semver version. Every
`EvaluationArtifact` row pins the full semver string. The three-level
semantics (major = score-incompatible, minor = additive, patch =
no-op for rankings) are defined in `docs/architecture.md` and
enforced by `src/evaluation/versioning.py`.

An evaluator major-version bump invalidates stored artifacts for that
evaluator. The invalidation is not destructive — old artifacts remain
in the DB tagged with the old version — but they do not count toward
the current evaluation run.
