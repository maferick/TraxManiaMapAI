# Evaluation Plan

**Status:** stub. Full content lands in PR 2. This file exists now so that
the evaluation-first discipline is visible from day one of the repo.

Evaluation is not an afterthought. Before any generator logic is written,
this document must define the evaluation contract.

## A. Product tracks

Two distinct product tracks exist. They must not be collapsed into a single
success metric.

### Mapper-assist (Phase 1)

Success means:

- route skeletons are interpretable
- outputs are editable
- outputs are useful starting points for human mappers

### Player-facing (later phase)

Defined here only to prevent architectural drift. Phase 1 does not ship
player-facing autonomy.

## B. Subsystem evaluation

To be filled in PR 2. Each of the following needs a benchmark strategy and
pass/fail thresholds:

- style classifier
- reward model
- flow / drivability surrogate
- structural validator
- adjacency graph validity
- route inference quality

## C. Human evaluation protocol

To be filled in PR 2. Must cover:

- two-tier reviewer model (small recurring core panel + larger occasional
  expert pool)
- pairwise baseline policy (what generated tracks are compared against)
- inter-rater agreement as a mandatory metric
- distinction between stated preference and behavioral preference
- explicit note: low inter-rater agreement means the target is ill-defined;
  behavioral signals may outrank mapper opinion for player-facing systems

## D. Diversity evaluation

To be filled in PR 2. Must include:

- pairwise similarity distribution across generated batch
- feature-space coverage vs training corpus
- repeated motif detection
- novelty floor

Diversity checks must exist before any generator work begins.

## E. Exit criteria

To be finalized in PR 2.

### Milestone A — data / evaluator viability

- ingestion success threshold (TBD %)
- replay cleaning coverage threshold (TBD %)
- benchmark reproducibility threshold (TBD)

### Milestone B — mapper-assist viability

- % of route skeletons rated useful by the panel (TBD)
- % of generated outputs passing structural + route sanity checks (TBD)

### Milestone C — competitive quality (later)

Must include **both**:

- acceptability threshold
- competitiveness threshold against community maps

"Acceptable" alone is not sufficient as final success.

## Kill-switch for Phase 1

If after PRs 1–4:

- ingestion success rate on 10k random maps falls below the Milestone A
  threshold, **or**
- replay cleaning rejects a larger fraction of replays than the Milestone A
  threshold allows,

pause route inference work and reassess. Thresholds to be agreed in PR 2.
