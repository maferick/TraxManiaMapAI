# Minimal AI generator v0

**Status**: normative. Gates the block-sequence generator's first
implementation. Changes to v0 require a revision bump
(`minimal-ai-generator-v0.1.md` etc.), not an in-place edit.

**Phase**: 2 (see `CLAUDE.md`).

**Relationship to `generation-scope-v0.md`:** orthogonal.
`generation-scope-v0.md` defines the artifact + finishability gate
for the *full-base-copy* generator (`generation-v0` / `v0.1`). This
doc defines a new generator mode (`generation-v0.2`) that
**synthesises** routes instead of copying base corridors. Both
produce compatible-shaped JSON artifacts; the schema grows one
`schema_version` enum and a handful of optional `map.*` fields to
carry the AI provenance. Everything else (finishability gate, run_id
semantics, provenance block) is identical.

## Purpose

Ship the first generator that builds a route **block-by-block**
rather than reusing an enumerated corridor. It reads the corpus
signals already in MariaDB and writes a `generation-v0.2` artifact
that the finishability gate can verify. No transformer, no RL, no
scratch-from-nothing — the base map still supplies the anchor
positions (Spawn, Checkpoints, Goal).

Why ship this before training a model:

- **It closes the loop end-to-end.** Artifact → validators → gate.
  Any quality issue becomes a concrete signal to target.
- **It eats existing signals.** Pair / triple transitions, block
  geometry, traversability, learned corridor scores, sequence
  scores, validation scores — already persisted, already versioned.
  The generator is a lookup-and-score loop, not a new learning
  problem.
- **It's a baseline.** Future ML models compete against it. Any
  generator that can't beat weighted greedy on the frozen benchmark
  isn't worth shipping.

## Non-goals for v0.2

**Do not add to v0.2 without a revision bump.**

- **No transformer / sequence model training.** Scoring is a linear
  combination of existing signals.
- **No reinforcement learning.** No self-play, no reward-signal
  training loop.
- **No scratch generation.** The base map supplies anchor
  positions and the environment / collection. Scratch requires
  choosing an environment AND synthesising a whole anchor layout,
  which is a separate phase.
- **No OpenPlanet dependency.** All signals come from MariaDB
  tables already populated by the ingestion pipeline.
- **No free-block synthesis.** Grid blocks only. Free-placed
  anchors from the base map are preserved (they carry Spawn / CP
  / Goal positions the gate needs) but the generator never *emits*
  a new free block.
- **No GBX emit.** The existing `emit-gbx` CLI mutates a base
  `.Map.Gbx` file; it does not know how to *build* one from a
  block list. v0.2 is JSON-only. A separate PR teaches the GBX
  wrapper how to construct a map.gbx from a synthesised block
  list (`emit-map-from-blocks`). Until then, generated routes can
  be inspected in Flask but not driven in-game.
- **No plain-CP support.** Same reason as `generation-scope-v0.md`:
  plain-CP interval ordering is ambiguous until per-CP alignment
  or OpenPlanet telemetry is available.
- **No cross-environment generation.** Stadium-only for v0.2
  (matches the corpus distribution).

## Algorithm

### Per-run inputs

- `base_map_id` — supplies anchors (Spawn, CPs, Goal), collection,
  environment. Must be Linked-CP; plain-CP short-circuits with
  `reject_reason=plain_cp_not_supported_v0`.
- `random_seed` — deterministic over the (seed, map, corpus-snapshot)
  tuple. Same triple → same `run_id` → bit-identical artifact.
- `beam_width` (default 3) — top-K candidates kept per step.
- `max_interval_depth` (default 12) — hard cap on blocks per
  interval. Intervals that can't reach the destination within
  this budget become `reject_reason=beam_exhausted`.

### Per-interval loop

Given anchor pair `(src, dst)` with cells `Cₛ`, `C_d`:

1. Initialise the beam: one partial path starting at `Cₛ` with
   the source anchor block.
2. While no beam has reached `C_d` (Chebyshev ≤ 1) and depth ≤
   `max_interval_depth`:
   a. Expand each beam: enumerate **candidate next blocks** —
      blocks in the catalogue whose `connector_hint` admits entry
      on the current beam's exit axis.
   b. Score each candidate (formula below).
   c. Reject candidates that would place a block at an occupied
      cell or outside the map's existing bounding box expanded by
      a small margin.
   d. Keep the top `beam_width` expansions across all beams.
3. If any beam reached `C_d`: pick the highest-scored.
4. Else: return `reject_reason=beam_exhausted` for this interval.

### Scoring formula

For a candidate `B_next` at cell `C_next`, entering from current
block `B_cur` at cell `C_cur`:

```
score(B_next) =
    w_pair  × P(B_next | B_cur)                        # pair transition prior
  + w_triple × P(B_next | B_prev, B_cur)               # triple transition prior
  + w_geom  × connector_match(B_cur, B_next)           # 0/1 compatibility
  + w_trav  × traversable(B_cur, B_next)               # 0/1 graph edge
  + w_seq   × combined_sequence_score_for(B_cur, B_next)
  - w_div   × diversity_penalty(B_next, path_so_far)
  - w_val   × (1 - validation_score_for_step)          # pre-validator slice
```

- **w_pair** = 1.0 — strongest prior; trained on the whole corpus.
- **w_triple** = 0.7 — sharpens pair when context available.
- **w_geom** = 0.5 — connector hint match is a hard prior but
  coarse (ramp_xy matches ramp_xy regardless of specific block).
- **w_trav** = 0.5 — traversability graph evidence.
- **w_seq** = 0.3 — pattern/geometry compatibility (#218-5).
- **w_div** = 0.3 — diversity penalty; count of this
  `(family, name)` already placed in this interval divided by
  interval length.
- **w_val** = 0.4 — soft partial-validator penalty. Run
  `check_partial_multicell` + footprint shadow check on the
  proposed placement. Full geom + jump validators run once at
  end of interval.

All weights pinned in `AI_GENERATOR_WEIGHTS` (`src/generation/
ai_generator.py`); tuning happens via config override + version
bump on `ai_generator_version`.

### Candidate filtering

Before scoring, drop candidates whose:

- `is_deco=True` (decorations are not drivable)
- `shape_class='unknown'` (no inference → can't reason)
- `shape_class='support'` (pillars / bases aren't drivable)
- `connector_hint` is empty (can't compose into a route)
- `placement_mode='free_only'` (grid-only v0.2 scope)

Anchors (Start / Checkpoint / Finish) never appear as candidates —
those are supplied by the base map and preserved verbatim.

### Post-interval validation

After each interval's chosen path is fixed:

1. Run `validate_map_geometry` over the current cumulative block
   set + interval's route cells. If any `SEVERITY_FAIL` finding
   sits on this interval's cells, mark interval broken and retry
   the interval from the second-best beam if available; otherwise
   emit `reject_reason=interval_validation_failed`.
2. Run `validate_jumps` with `replay_touched_cells`. `likely_broken`
   classifications on this interval's cells trigger the same retry
   behaviour.

### Whole-route finalisation

After all intervals chain successfully:

1. Assemble `AssembledRoute` shape (same dataclass used by
   `generation-v0`) and hand it to
   `src.generation.finishability.run_finishability_gate`.
2. Compute `ai_confidence` as the mean of per-interval scores,
   normalised to [0, 1].
3. Build the `generation-v0.2` artifact.

Chain continuity between intervals is enforced by construction:
each interval starts at the previous anchor cell and ends at the
next. No additional continuity check needed.

## Artifact format (`generation-v0.2`)

Same top-level shape as `generation-v0.1`. Two optional `map.*`
fields added:

- `map.ai_generated`: `boolean` — `true` when the block list was
  synthesised by this generator; `false` / absent for base-copy
  artifacts.
- `map.ai_generator_version`: `string` — e.g. `"ai-generator-v0.0"`.
  Bumps when scoring weights or the algorithm change in a way
  that invalidates prior artifacts for A/B comparison.

The `schema_version` enum grows one value: `generation-v0.2`.
Readers that don't recognise the value reject the artifact —
there's no silent back-compat path for artifacts that advertise a
schema the reader doesn't implement.

Everything else — `inputs`, `provenance`, `route`, `finishability`,
`map.blocks`, `map.checkpoints` — matches v0.1.

## Persistence for debugging

Every chosen block records its per-signal score contributions.
Persisted in the artifact under `map.blocks[*]` as optional
fields (schema grows in a backward-compatible way):

- `ai_score`: `number` — the final weighted score at pick time.
- `ai_score_breakdown`: `object` — one entry per weight
  (`pair_prior`, `triple_prior`, `connector`, `traversability`,
  `sequence`, `diversity_penalty`, `validation_penalty`).

Blocks inherited from the base map (anchors preserved verbatim)
omit these fields. Makes Flask diff pages legible — every
synthesised block carries a full cost breakdown.

## Failure modes + `reject_reason` values

The existing `reject_reason` enum in `generated-map-v0.json`
grows to cover v0.2's new rejection paths:

- `beam_exhausted` — no beam reached the destination within
  `max_interval_depth`.
- `interval_validation_failed` — all beam alternatives produced
  a validator FAIL finding on the interval's cells.
- `no_valid_candidates` — filter dropped every candidate on some
  step (empty catalogue / restrictive classifier).

Existing reasons (`plain_cp_not_supported_v0`, `chain_broken`,
`missing_corridor_in_interval`, `unknown_block`, `invalid_schema`,
`stripped_route_broken`) are preserved and continue to apply where
relevant.

## Data dependencies

Tables the generator reads (no writes):

- `maps`, `map_checkpoints` — base anchors + collection.
- `block_placements` — base anchor blocks verbatim.
- `block_geometry` — catalogue of candidate blocks + their
  `shape_class` / `surface_hint` / `footprint_*` /
  `placement_mode` / `connector_hint`.
- `block_pair_transitions` — P(B_next | B_cur) priors.
- `block_triple_transitions` — P(B_next | B_prev, B_cur).
- `traversability_edges` — edge-level evidence.
- `route_corridors` — corridor_confidence for the replay-cell
  proxy (`load_replay_touched_cells`).
- `block_pair_transitions`, `block_triple_transitions` — combined
  sequence score components.

## Testing strategy

- Pure-function tests for the scorer (weights, connector match,
  diversity penalty).
- Beam-expansion tests with a synthetic catalogue.
- Integration test on a Linked-CP fixture map with a small block
  corpus.
- Smoke on map 1212 (Linked-CP fixture) — artifact validates
  against `generation-v0.2`, finishability gate runs, CLI exits 0.

Quality is **not** the first-PR gate. We're proving the pipeline
runs end-to-end; bad generated routes are expected and legible in
the artifact's `ai_score_breakdown` fields.

## Out-of-scope follow-ups (not this PR)

- GBX emit for v0.2 (teach wrapper `emit-map-from-blocks`).
- Quality tuning of weights (needs the frozen-benchmark PR).
- Multi-environment / style-aware generation.
- Plain-CP support.
- Scratch generation (no base map).
- ML model competing against this baseline on the same benchmark.
