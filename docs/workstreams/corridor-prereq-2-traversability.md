# Corridor Prereq 2 — Traversability Subgraph

## Purpose

This document defines **Prereq 2 — Traversability Subgraph** for the
corridor-inference workstream.

It expands on the parent charter:

- `docs/workstreams/corridor-inference.md`

and is the **authoritative source for traversability design**.

## Scope

Construct a **traversability subgraph** from the raw adjacency graph
representing:

> movement-feasible transitions between blocks, constrained by
> checkpoint anchors.

Enables:

- checkpoint-to-checkpoint corridor candidate generation
- branch coverage evidence
- graph sanity validation

Explicitly does **not** attempt:

- racing-line reconstruction
- trajectory inference
- difficulty estimation
- replacing telemetry

## 1. Adjacency vs traversability

### Adjacency

- purely geometric neighbor relation
- derived from block placement
- includes deco / support / non-drivable neighbors

Adjacency = **structural proximity**.

### Traversability

- subset of adjacency representing plausible car movement
- constrained by:
  - block family
  - orientation
  - elevation continuity
  - local structure

Traversability = **movement-feasible proximity**.

### Rule

All traversable edges are adjacent.
Not all adjacent edges are traversable.

## 2. Design constraints (inherited)

From the parent charter:

- No circular inference from input-derived signals.
- No promotion of validity from frequency patterns.
- No reliance on noisy style tags.
- Must function without position telemetry.

## 3. Inputs

Available:

- `block_placements`
- adjacency graph (Neo4j)
- `map_checkpoints` (971 maps, ~29k anchor rows, per
  `migrations/mariadb/015_map_checkpoints.sql`)
- replay breadcrumbs:
  - input events
  - checkpoint timing
- cohort membership
- enrichment metadata (style tags, difficulty, TrackValue,
  scenery)

Not available:

- position telemetry
- authoritative connector / surface model
- ground-truth trajectories

## 4. Traversability model

### 4.1 Phase separation (critical)

**Phase 2 (seed rules) MUST prune the graph before any path
enumeration.**

No enumeration is allowed over raw adjacency. The chicken-and-egg
problem ("which edges are traversable for enumeration?" vs
"enumeration tells us which edges are traversable") is broken by
forcing the deductive seed rules to run first and act as the
enumeration substrate.

### 4.2 Seed traversability rules (deductive)

Conservative initial rules, hand-authored per block family.

Allowed examples:

- road → road, aligned, same plane
- dirt → dirt, continuous
- ice → ice, same plane
- platform → platform, co-planar
- checkpoint block → aligned same-surface continuation

Disallowed examples:

- track → decorative wall
- track → support pillar
- deco → deco (default disallow)
- large vertical discontinuities without known connecting geometry

Goal of Phase 2:

> reduce the adjacency graph to a tractable candidate graph over
> which path enumeration can run.

### 4.3 Inductive evidence (non-validity)

Evidence is used **only for weighting and pruning**, never for
validity promotion. This keeps `src/constraints/evidence.py`'s
"frequency is NOT validity" invariant intact.

#### Signal 1 — checkpoint-anchored path plausibility

For each interval `{anchor_set_A} → {anchor_set_B}`:

- enumerate paths over the **seed graph only** (per §4.1)
- score paths by:
  - length plausibility (vs. checkpoint time envelope, see Signal 2)
  - family continuity
  - orientation consistency

Edges recurring in plausible paths gain weight in the
`path_support_count` column of the evidence artifact.

#### Signal 2 — route-length plausibility only (non-circular)

Checkpoint timing constrains the envelope of feasible path lengths
between two anchors. That is the full extent of its role.

**Allowed:**

- `elapsed_ms` between CPs constrains max plausible path length
  given a conservative speed prior (Phase 3 of the corridor
  workstream will establish this prior).

**Not allowed:**

- inferring transition complexity from input density
- inferring difficulty from steering patterns

Rationale: input density IS the evidence of complexity; using it to
infer complexity and then feeding that inference back as support is
circular.

#### Signal 3 — cross-map transition patterns (bounded)

**Allowed:**

- identify globally common transitions (`family_A → family_B, same
  rotation`) as a weak prior weight
- contributes to `pattern_weight`

**Not allowed:**

- promoting edge validity via frequency
- feeding pattern weights into constraint graph
  `replay_supported_count`

Policy:

> pattern-based evidence stays within the traversability graph;
> only replay-observed crossings bump constraint-graph validity
> (see §6.3).

#### Signal 4 — local structural suppression

Downweight edges in:

- deco clusters
- support / foundation structures
- disconnected subgraphs with no track-family membership

Contributes to `negative_evidence_count`.

#### Style-conditioning is out of scope

TMX style tags are noisy (see `docs/benchmark-policy.md`), so
traversability weighting is NOT conditioned on style. A later
workstream may revisit once a hand-curated style partition exists.

## 5. Checkpoint anchor handling (multi-cell aware)

A single logical checkpoint may map to multiple blocks — the
empirical finding from landing Prereq 1 is that multi-cell gates
like `GateExpandableFinish` span up to 12 adjacent cells, each with
its own waypoint row in `map_checkpoints`.

### Representation

Each checkpoint is represented as:

```
anchor_set = { block_id_1, block_id_2, ... }
```

a plain set of candidate anchor blocks. No forced single-anchor
selection; ambiguity is preserved, not collapsed.

### Path search

Search over an interval is defined as:

```
ANY path from anchor_set_A → anchor_set_B
```

not a single source-target pair. The path-enumeration implementation
must handle many-to-many search.

### Implications

- path explosion risk is real → strong seed pruning is mandatory
  (§4.1 is load-bearing)
- ambiguity is preserved, not collapsed to a single block
- multi-cell dedup stays the responsibility of the consumer (per the
  existing `migrations/mariadb/015_map_checkpoints.sql` policy)

## 6. Output artifacts

### 6.1 `traversability_edge_evidence`

Per-map-scoped evidence table. Columns:

- `map_id`
- `src_block_id`
- `dst_block_id`
- `rule_support` (bool — matched a Phase 2 seed rule)
- `path_support_count` (Signal 1 hits)
- `pattern_weight` (Signal 3 contribution)
- `negative_evidence_count` (Signal 4 hits)
- `traversability_state` ∈ `{seed_valid, supported, unsupported,
  unknown}`

`map_id` is required because traversability is evaluated per-map —
the same pair of block families in different contexts may have
different states. A global-across-maps aggregate view can be
materialized later from this base table.

### 6.2 `traversability_subgraph`

Filtered graph used for:

- corridor candidate search
- branch pruning
- reachability validation

Defined as the subset of `traversability_edge_evidence` rows where
`traversability_state ∈ {seed_valid, supported}`.

### 6.3 Constraint-graph interaction (policy-safe)

Only this interaction with `src/constraints/evidence.py` is
permitted:

- Edges with **explicit replay-crossing evidence** (a specific
  replay's breadcrumbs demonstrably traversed that specific edge
  during a race) may increment `replay_supported_count`.

Not permitted:

- pattern frequency → validity
- inferred path plausibility → validity
- any traversability-graph state → validity

This preserves the existing validity policy:

> `benchmark_strong_count ≥ 1` → `valid`;
> `broken_fixture_count > 0` AND no positive evidence → `suspicious`;
> everything else (including high `observed_in_maps_count`) →
> `unknown`.

## 7. Validation

All validation runs against the **fixed 10-map validation set**
defined in §7.4 unless otherwise noted. Full-corpus (971-map)
validation is out of scope for Prereq 2 — the 10-map set is the
commit bar.

### 7.1 Reachability

For each checkpoint interval in the 10-map set:

- at least one path must exist under the traversability subgraph
- failure modes to distinguish:
  - missing seed rule
  - bad anchoring
  - missing adjacency data

### 7.2 Decorative suppression

Compare raw adjacency vs traversability subgraph on the 10-map set:

- % of deco/deco edges removed
- % of support / foundation edges removed
- % of track-family edges retained

### 7.3 Cross-replay consistency

For maps in the validation set that have ≥3 clean breadcrumb-path
replays:

- do the same checkpoint intervals repeatedly favor similar path
  families / branches?

Consistency is not a validity proof — it's a stability signal.

### 7.4 Fixed manual validation set

10 maps, style-balanced across what `2026-04-scale-1k` actually
contains:

- Tech: 4 maps
- Dirt: 3 maps
- Ice: 3 maps
- At least 3 maps with `LinkedCheckpoint` structure

For each map in the set, manually validate:

- anchor-block correctness
- per-interval reachability
- absence of obviously-false corridors

### 7.5 Downstream usefulness

Does the traversability graph:

- populate `replay_supported_count` on genuine crossings
- narrow branch coverage to plausible candidates
- make corridor search tractable in practice

## 8. Phase 1 success gate (commit bar)

All of the following must hold before Prereq 2 is declared complete.
Gates run against distinct validation sets because they measure
different things and benefit from different data — mixing them on a
single set distorts the measurement.

### §8.1 Interval reachability — on V2

Target set: `VALIDATION_MAP_IDS_V2` (data-coverage-aware: maps with
≥3 clean breadcrumb replays in the pinned snapshot, so the
observation-augmented reachability path has something to work with).

Pass: ≥90% of non-spawn checkpoint-interval anchor sets reachable
from the spawn component on the combined (seed_valid ∪ observation-
asserted) traversability subgraph.

### §8.2 Deco/support suppression — on V1

Target set: `VALIDATION_MAP_IDS_V1` (structural-diversity set, deco-
heavy maps originally calibrated for the 80% suppression threshold).

Pass: ≥80% of raw-adjacency edges on the V1 set are excluded from
the traversability subgraph (either `unsupported` or `unknown`).

Rationale for split: V2 maps are typically smaller tech/plasti
maps with little deco volume — on V2, the 80% threshold is
unachievable not because pruning is weak but because there's
nothing to prune. Measuring suppression on V1 (deco-heavy) and
reachability on V2 (data-coverage) keeps each metric on the data
it was designed for.

### §8.3 Automated sanity validation — on V2

**Note on framing:** the original §8.3 asked for manual review.
Hand-curated review is unavailable for this workstream phase.
Therefore §8.3 is temporarily satisfied by automated sanity checks
only. This is sufficient for advancing the traversability /
corridor substrate. It is **not** sufficient to claim human-
confirmed corridor quality. Human review remains deferred future
work. Do not describe the automated substitute as "manual
validation passed by proxy"; describe it as "manual validation
unavailable; automated sanity gate substituted."

Pass: ALL of the following on the V2 validation set:

1. **Zero unsupported-edge contamination.** No enumerated
   corridor path contains an edge whose `traversability_state` is
   `unsupported`. Tautological by construction if path enumeration
   runs on the seed_valid subgraph only; becomes load-bearing if
   future phases relax the subgraph. Enforced as a post-
   enumeration check.
2. **Zero non-drivable family intrusion.** No cell on any selected
   corridor has a `block_family` in `NON_DRIVABLE_FAMILIES`.
   Same construction-vs-post-check story as (1).
3. **Deco-adjacent contamination below threshold.** The
   fraction of corridor cells with a deco/support family cell as
   a grid neighbor is below 0.40. Catches corridors that thread
   through drivable-but-deco-rimmed chokepoints where the
   classification happens to be correct but the route sits in a
   non-raceable neighborhood.
4. **Path-family stability under perturbation.** For each interval
   with ≥2 clean replay observations, the top-ranked corridor
   (by edge count, ties broken lexicographically by endpoint coord)
   under the full observation set must equal the top-ranked
   corridor under a re-run with a single replay held out. Measures
   whether observation additions are additive-stable or cause
   wild-swing re-rankings.

### §8.4 Path-enumeration tractability — on V2

Pass under depth-10 DFS enumeration on V2:

- median interval has ≤ 1,000 candidate paths (central-tendency)
- p95 interval has ≤ 10,000 candidate paths (explosion control —
  catches the long-tail interval that would otherwise blow up the
  whole validation run)

### Overall policy

If any of §8.1–§8.4 is not met: do not declare Prereq 2 complete.
Return to Phase 2 / 3 with the specific failure evidence in hand
and iterate before re-running validation.

When manual validation eventually becomes available, §8.3 should
be re-tightened to include hand-curated review in addition to (not
in place of) the automated checks. Until then, the four automated
checks above ARE the gate.

## 9. Implementation phases

### Phase 1 — classification + audit

- enumerate block families observed in the 999-map corpus (expected
  to be in the low hundreds of distinct family names)
- classify each family into:
  - drivable
  - potentially drivable
  - non-drivable
  - decorative / support
- inspect checkpoint anchors on the 10-map validation set by hand
- define the evidence schema (§6.1) as a concrete migration

Classification is explicitly high-maintenance — Nadeo ships new
blocks with client updates. Phase 1 sets the initial classification
and the review cadence (suggested: re-classify after each new TM2020
release that adds blocks).

### Phase 2 — seed rules (required before anything else)

- implement the conservative rule-based subgraph using Phase 1
  classification
- populate `traversability_edge_evidence.rule_support`
- run reachability checks against the validation set
- iterate on rules until §8.1 passes

### Phase 3 — inductive weighting

- add Signal 1 (path-support) aggregation
- add Signal 2 (time-envelope) pruning
- add Signal 3 (pattern-weight) prior
- add Signal 4 (structural suppression)
- all writes go to the non-validity columns of the evidence table

### Phase 4 — validation

- run every validation mode in §7
- evaluate against the §8 gate
- if the gate fails: do not declare Prereq 2 complete; return to
  Phase 2 / 3 with the specific failure evidence and iterate

## 10. Risks

- **Block classification maintenance cost.** High, ongoing. Mitigation:
  a coverage check in the pipeline that flags unclassified families,
  plus a cadence review aligned to Nadeo release notes.
- **Adjacency noise dominating early graph.** The raw graph has
  deco / support neighbors everywhere; if Phase 2 seed rules don't
  prune aggressively, Phase 3 enumeration won't be tractable.
- **Anchor ambiguity causing path explosion.** Multi-cell gates
  multiply the effective anchor count per interval. Mitigation:
  enumeration depth cap + early termination on first feasible path.
- **Lack of telemetry limiting validation strength.** All validation
  is either structural (§7.1–§7.2) or consistency-based (§7.3) —
  none proves geometric correctness. The OpenPlanet workstream
  remains the only path to true validation.

## 11. Summary

Traversability is a constrained, evidence-backed subgraph of
adjacency that represents movement-feasible transitions between
checkpoint anchors. It is built via conservative deductive rules
first, then refined with bounded inductive evidence, and validated
against a fixed 10-map set using reachability, decorative
suppression, and cross-replay consistency. It does not reconstruct
motion or difficulty. It preserves the existing constraint-graph
validity policy by only promoting replay-observed crossings; all
other evidence stays within the traversability graph.
