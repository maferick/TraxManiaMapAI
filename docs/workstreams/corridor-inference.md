# Workstream — Corridor inference from breadcrumbs + block graph

## Status

**Open, unstaffed.** Parked as a valid future direction that does NOT
require full position telemetry to unlock. Not the current critical
path; the breadcrumb-only replay-cleaning redesign (see roadmap PR 5
status) runs first because it's the prerequisite that turns our
breadcrumb-bearing replays into cohort-assignable inputs.

This is a companion workstream to
`docs/workstreams/openplanet-telemetry.md`. Openplanet delivers
continuous truth (per-tick position + velocity). Corridor inference
delivers constrained inference (plausible block chains between
checkpoints with timing + input evidence). They complement each
other — corridors become priors for the Openplanet pipeline when it
lands.

## Goal

Produce a per-replay, per-checkpoint-interval artifact that names the
most plausible sequence of traversed blocks between consecutive
checkpoints, together with a calibrated confidence score.

Artifact shape (proposed, not committed):

```
route_corridor_artifact v1
  replay_id
  map_id
  corridor_version
  intervals[]:
    cp_from_index      # int; 0 = start
    cp_to_index        # int
    elapsed_ms         # observed breadcrumb timing
    block_chain[]      # ordered block ids from our block_placements
    chain_cost         # graph-derived path cost
    timing_plausibility # [0..1]; does elapsed_ms fit a block-traversal speed prior?
    input_consistency  # [0..1]; input density/event pattern consistency
    confidence         # aggregate [0..1]
  diagnostics
```

## Non-goals

- **Racing-line reconstruction.** Corridor ≠ position. A corridor
  names the blocks traversed; the exact path within those blocks is
  unrecoverable without real telemetry.
- **"Segment difficulty" / "intent" inference from inputs.** Deriving
  "technical region" from "high steer density" is circular — steering
  IS the evidence. Evaluators must not use one to confirm the other.
- **Speed profiles, apex behavior, drift angles, landing trajectories.**
  All require continuous position.
- **Single-replay-per-map claims.** Corridor inference is a *cross-
  replay* capability — single-replay outputs are fine as artifacts but
  evaluation claims must aggregate over multiple replays on the same
  map.

## Prerequisites (not free)

None of the following exist today. The workstream cannot begin until
at least prerequisites 1 and 2 land.

### 1. Checkpoint → block mapping

TM2020 checkpoints are MediaTracker triggers defined inside the map
GBX, not grid blocks. The wrapper extracts block placements but not
checkpoint trigger positions. Unblocking requires:

- extending `parsers/gbx-wrapper/MapParser.cs` to extract checkpoint
  trigger `(x, y, z)` from the map GBX (spike needed — location in
  GBX.NET API is TBD);
- snapping each trigger coord to its nearest block in
  `block_placements` (deterministic; straightforward once positions
  are available).

Output: a new table `map_checkpoints (map_id, cp_index, block_id,
trigger_x, trigger_y, trigger_z)`. Written by a new pipeline stage.

### 2. Traversability subgraph

Our current Neo4j adjacency graph is grid-neighbor-based: any two
blocks sharing a face are `ADJACENT_TO`. But neighbors aren't always
drivable between — a wall next to a platform is a neighbor, not a
path. Corridor inference over the raw graph produces geometric
neighbors, not racing routes.

Producing a traversability subgraph requires a **drivability model
per block type / variant**. Options:

- **Inductive from replay breadcrumbs (once we have many clean
  replays):** an edge `A → B` is traversable iff ≥N clean replays
  that pass through the (A, B) neighborhood are temporally
  consistent with driving across it. Self-bootstrapping, needs data.
- **Deductive from block-family surface rules:** a hardcoded rule
  set ("Tech blocks have drivable top surface; DirtRoad has
  drivable everywhere; Fullspeed variants connect only to matching
  entry/exit faces"). Authoritative if we can build it, but
  expensive; rule set has to be maintained as Nadeo ships new
  blocks.
- **Hybrid:** deductive rules for confidence-heavy edges, inductive
  for the rest. Recommended starting point.

This is the single biggest scoping question. Doing it wrong makes
corridor output look plausible but be wrong in systematic ways.

### 3. Speed / cost model for block traversal

Pathfinding needs an edge weight to be useful. A per-block-family
speed prior (e.g. mean observed traversal time per block type across
clean replays) is the minimum viable model. Without it, "shortest
path" devolves to "fewest blocks," which isn't what drivers pick.

This prior must be **re-estimated periodically** as the clean-replay
corpus grows. It's pipeline-versioned, not static.

### 4. Validation corpus

No single-replay validation is rigorous — the only ground truth is
position telemetry we don't have. Validation paths:

- **Cross-replay consistency (interim).** For maps with ≥10 clean
  breadcrumb-bearing replays, corridor inference should converge on
  the same block chain for a strong majority (say ≥0.8 agreement).
  Maps where it doesn't are flagged for graph repair or manual
  inspection.
- **Openplanet-backed validation (gold, when available).** Once the
  Openplanet workstream delivers position telemetry on a benchmark
  map, we can directly score corridor accuracy. This is the only
  validation path that closes the loop.

## Interface to the rest of the system

- **Depends on:** `block_placements` (exists), PR 6 constraint graph
  (exists), breadcrumb sidecars (exists as of commit bc560f7), clean
  breadcrumb replays (NOT YET — blocked on the breadcrumb-cleaning
  redesign).
- **Produces:** a new artifact type, content-hash addressed on disk;
  a new table `route_corridors` mirroring the shape of the existing
  `route_artifacts` but with different semantics.
- **Feeds:**
  - a new evaluator `route_corridor@0.1.0` scoring corridor
    confidence and cross-replay consistency (NOT racing-line
    fidelity);
  - the existing `replay_supported_count` evidence field on
    constraint-graph edges. Every corridor interval contributes
    `+1` to the `replay_supported_count` of each
    `(block_n → block_n+1)` transition in its chain. This is the
    first real use of that field — up to now it was defined in the
    evidence policy but never populated.
- **Does NOT feed:** `route_artifacts` (centerline reconstruction),
  `route_coverage@0.1.0` (that evaluator surfaces
  `extraction_confidence` from continuous routes, not corridors).

## Success criteria

Before this workstream can claim "done" for Phase 1 substrate:

1. Corridor inference runs on ≥5 benchmark fixture maps each with
   ≥10 clean breadcrumb replays and produces artifacts that validate
   against the schema.
2. Cross-replay consistency on those fixture maps is ≥0.8 (or the
   discrepancies are explained by known branch structure).
3. The `replay_supported_count` field on the constraint graph
   actually gets populated, and the `validity_label` policy in
   `src/constraints/evidence.py` is revisited — the policy comment
   already foreshadows letting clean-cohort replays upgrade edges to
   `valid` once replay-to-block projection exists. Corridor inference
   is that projection; the policy update is explicitly in scope for
   this workstream's landing.
4. The evaluator `route_corridor@0.1.0` emits scores on the PR 7
   dry-run and separates the strong/mediocre benchmark sets with
   AUC > 0.6 — OR we produce a principled explanation of why
   corridor confidence doesn't separate those cohorts, which is
   itself a useful finding.

If (4) yields AUC ≈ 0.5, that means either our cohorts aren't
quality-distinguished (likely — the current proxies are popularity-
based), OR corridor confidence isn't a quality signal. Both
conclusions are shippable; "didn't work" with a clear explanation is
still a Phase 1 deliverable.

## Risks

- **Checkpoint-to-block mapping precision.** If the nearest-block
  match for a trigger is wrong by one block, downstream pathfinding
  starts from the wrong seed and every corridor is slightly off.
  Mitigation: validate the mapping on hand-labeled fixtures before
  letting it drive corridor outputs.
- **Traversability model rot.** Every Nadeo update may ship new
  blocks. If the drivability rule set isn't re-evaluated, corridors
  on new maps degrade silently. Mitigation: block-family coverage
  check in the pipeline + periodic audit.
- **"Looks plausible but is systematically wrong."** A corridor can
  be graph-valid and input-consistent yet not the actual path. This
  is the most dangerous failure mode because it doesn't surface in
  validation. Mitigation: compare against Openplanet ground truth
  the moment that workstream delivers any data, even on one map.
- **Scope creep into intent/difficulty.** Described in non-goals; the
  risk is that once corridors work someone will want to label them
  with "technical" / "flowing" / "jump-heavy" — which the non-goals
  explicitly reject as circular. Pre-commit hook or review-time
  discipline.

## Handoff checklist

Start from:

1. `docs/reverse-engineering/tm2020-replay-telemetry-spike.md` —
   establishes why position telemetry is offline-blocked.
2. `docs/workstreams/openplanet-telemetry.md` — the companion
   workstream that delivers continuous truth (long-term).
3. `src/constraints/evidence.py` — the `replay_supported_count`
   field this workstream is the first to populate, and the comment
   foreshadowing the `validity` policy update.
4. `parsers/gbx-wrapper/ReplayParser.cs` — current breadcrumb export
   shape.
5. `src/route/pipeline.py` — the existing `RoutePipeline` handling
   centerline artifacts. Corridor inference is a *different* artifact
   type that should NOT be shoehorned into this pipeline — write a
   new `CorridorPipeline` to keep the artifact semantics separate.

First deliverable is prerequisite 1: extend the map parser to emit
checkpoint trigger positions and land them in a new `map_checkpoints`
table. Without that, nothing else in this workstream can start.
