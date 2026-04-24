# Generation scope v0

**Status**: normative. This document gates all generation code —
no generator implementation PR is accepted that conflicts with
what's pinned here. Changes to v0 require an explicit revision
bump (`scope-v0.1.md`, etc.), not an in-place edit.

**Phase**: 2 (see `CLAUDE.md` for phase sequencing).

## Purpose

Define exactly four things for the first generation implementation:

1. **What a generated map artifact looks like** (the JSON schema).
2. **How its full route is assembled** (the algorithm, step by step).
3. **What "verified finishable" means** (the gate, the fields it
   returns, the rejection reasons).
4. **What is explicitly out of scope for v0** (so follow-up PRs
   know where the line is).

Anything not listed here is not v0. Proposals to expand v0 are
fine but must land as a separate design-doc revision, not quietly
inside an implementation PR.

## Non-goals for v0

Do **not** add to v0 without a revision bump:

- GBX output (JSON-only in v0; GBX is a later phase)
- Plain-CP map support (Linked-CP only — see constraint below)
- Item / scenery / free-placement blocks (grid blocks only)
- Style-blending beyond style-tag corpus filtering
- Difficulty parameters that modify physics or gameplay rules
- Multi-route exploration / variations / "lock route" UI
- Interactive regeneration loops
- Cohort-aware generation (don't filter by replay cohorts)
- Direct use of `observed_traversable` evidence in the finishability
  gate (see `project_replay_ground_truth_contract.md` memory;
  replays inform classification, gate uses that classification, the
  gate's logic itself doesn't change)

## Constraint: Linked-CP maps only

**v0 generates and validates only Linked-CP maps.**

A Linked-CP map carries one or more `map_checkpoints` rows with
`tag='LinkedCheckpoint'` — a dedicated tag the parser assigns when
the GBX waypoint declares an explicit chain order. Plain-CP maps
(`tag='Checkpoint'` with no chain order) merge into a single anchor
set during route enumeration and have no deterministic "next
interval" — route chaining cannot be done deterministically on them
in v0.

The constraint is **temporary**. It lifts when either:
- Per-CP alignment becomes available (e.g. matching observed
  `checkpoint_times_ms` to specific CP blocks via spatial
  proximity), or
- OpenPlanet per-frame telemetry lets us reconstruct CP order
  from driven paths.

Until then, plain-CP generation returns `route_verified=False`
with reason `plain_cp_not_supported_v0`.

**How to detect**:

```sql
SELECT 1 FROM map_checkpoints
WHERE map_id = ? AND tag = 'LinkedCheckpoint'
LIMIT 1;
```

Any row → treat as Linked-CP. Mixed-shape maps (both `Checkpoint`
and `LinkedCheckpoint` present on one map) fall back to plain-CP
so downstream keys stay consistent with Phase 1 training data.

**Chain anchor ordering**: sort the map's `LinkedCheckpoint` rows by
`waypoint_order` ascending (ties broken by `waypoint_index`). One
logical CP may span multiple cells — the parser emits one row per
cell, all sharing the same `(tag, waypoint_order)`. Dedupe on
`(tag, waypoint_order)` before building intervals or you'll look for
a self-interval (`LCP#1 → LCP#1`) that doesn't exist.

## Generated map artifact: JSON schema

### Top-level shape

Every generator run emits one JSON document. File naming:
`generated_map_<run_id>.json` (run_id is sha-256 of the
generator inputs, truncated to 16 chars — same pattern as training's
model_hash).

```json
{
  "schema_version": "generation-v0",
  "run_id": "a1b2c3d4e5f6a7b8",
  "generated_at": "2026-04-23T10:15:23+00:00",
  "inputs": {
    "base_map_id": 1042,
    "base_map_source_id": "108079",
    "style_tag_filter": "Tech",
    "difficulty": "medium",
    "random_seed": 42
  },
  "provenance": {
    "model_hash": "12cc0e58c19a...",
    "learned_score_version": "time_envelope_v2_weighted@0.1.0",
    "config_hash": "deadbeef...",
    "code_version": "abc1234",
    "classification_version": "0.1.0"
  },
  "map": {
    "waypoint_order_style": "linked",
    "interval_count": 3,
    "blocks": [ ... ],
    "checkpoints": [ ... ]
  },
  "route": {
    "intervals": [ ... ],
    "cells_total": 42,
    "corridors_used": [ ... ]
  },
  "finishability": {
    "route_verified": true,
    "estimated_time_ms": 24300,
    "ai_confidence": 0.74,
    "reject_reason": null,
    "gate_version": "finishability-v0"
  }
}
```

### Field semantics

- **`schema_version`**: literal `"generation-v0"`. v0 readers reject
  any other value. Follow-up phases bump to `"generation-v0.1"`,
  etc.
- **`run_id`**: 16-hex reproducibility handle. Same inputs (incl.
  `random_seed`) produce same `run_id`.
- **`inputs`**: fully capture what the operator asked for. At least:
  - `base_map_id` — integer maps.id or `null` if scratch.
  - `base_map_source_id` — TMX id or `null`; carried for UI.
  - `style_tag_filter` — one of `Tech` / `FullSpeed` / `null`. v0
    supports these two tags plus `null` (no filter). Other tags
    are a follow-up phase.
  - `difficulty` — one of `easy` / `medium` / `hard`. v0 uses this
    only to bias corridor selection thresholds (longer corridors →
    harder); does not tune physics or block selection.
  - `random_seed` — integer used for any tie-break or sampling.
- **`provenance`**: the five fields required for reproducibility.
  `learned_score_version` is the currently-deployed scheme tag
  (read from `route_corridors.learned_score_version`).
  `model_hash`, `config_hash`, `code_version` follow existing
  conventions. `classification_version` is the traversability
  classification the corridor graph was built under.
- **`map`**: the generated layout.
  - `waypoint_order_style`: literal `"linked"` for v0.
  - `interval_count`: number of Spawn→CP1→CP2→...→Goal intervals
    in the chain. Matches `len(route.intervals)`.
  - `blocks`: ordered list of block placements. Each element is:
    ```
    {
      "block_family": "RoadTech",
      "block_name": "RoadTechStraight",
      "x": 12, "y": 24, "z": 16,
      "rotation": 0
    }
    ```
    Grid cells only in v0. `block_name` references the canonical
    TMX block catalogue; unknown names in v0 reject the artifact
    (the generator's own validation, before finishability even
    runs).
  - `checkpoints`: ordered list matching `map_checkpoints` schema:
    ```
    {
      "waypoint_index": 0,
      "waypoint_order": 1,
      "tag": "LinkedCheckpoint",
      "x": 18, "y": 24, "z": 16
    }
    ```
    v0 requires every chain checkpoint to carry
    `tag="LinkedCheckpoint"` (the Linked-CP constraint).
    Free-placed waypoints (NULL grid coords in `map_checkpoints`) are
    omitted from `map.checkpoints`; their snapped cells still flow
    through `route.intervals` and `route.corridors_used`.
- **`route`**: the assembled route (see "Route assembly" below).
- **`finishability`**: the gate verdict (see "Finishability" below).

### Interval entry shape

```json
{
  "index": 0,
  "src_tag": "Spawn", "src_order": 0,
  "dst_tag": "LinkedCheckpoint", "dst_order": 1,
  "chosen_corridor_id": 12345,
  "chosen_corridor_score": 0.68,
  "path_length_cells": 14,
  "expected_time_ms": 11700
}
```

### Corridor-used entry shape

Summary of each corridor chosen for the route, so a reviewer
can reproduce scoring without re-running the pipeline.

```json
{
  "corridor_id": 12345,
  "interval_index": 0,
  "learned_corridor_score": 0.68,
  "corridor_confidence": 0.82,
  "contains_virtual_edge": false,
  "path_length_cells": 14
}
```

## Route assembly

The algorithm for picking the route is pinned to keep
implementations consistent across future refactors.

### Inputs

- A generated map's `route_corridors` rows (for a scratch map, the
  generator writes these rows before invoking assembly).
- The map's Linked-CP anchor sequence (derived from
  `map_checkpoints` ordered by `waypoint_order`).

### Algorithm

```
1. Enumerate the anchor sequence (dedupe by (tag, waypoint_order);
   a multi-cell CP emits several rows, all the same logical anchor):
     A0 = (Spawn, 0)
     A1 = (LinkedCheckpoint, 1)
     A2 = (LinkedCheckpoint, 2)
     ...
     An = (Goal, 0)
   Precondition: every chain checkpoint row uses
   tag='LinkedCheckpoint'. A plain 'Checkpoint' row (or a mixed
   Checkpoint + LinkedCheckpoint shape) → reject with reason
   `plain_cp_not_supported_v0`.

2. For each interval (A_i → A_{i+1}):
     a. Fetch all route_corridors rows with
          src_tag=A_i.tag, src_order=A_i.order,
          dst_tag=A_{i+1}.tag, dst_order=A_{i+1}.order
        for this map_id and the current classification_version.
     b. Filter to rows with learned_corridor_score NOT NULL.
     c. Sort survivors by (learned_corridor_score DESC, path_length
        ASC, corridor_id ASC) — the scope-pinned tie-break.
     d. Pick one corridor from the top K of that ordering. Which one
        is deterministic from (random_seed, interval_index):
           k       = min(TOP_K_CANDIDATES, len(survivors))
           payload = f"{random_seed}:{interval_index}".encode()
           pick    = int.from_bytes(
                         blake2b(payload, digest_size=8).digest(),
                         "big",
                     ) % k
        TOP_K_CANDIDATES is pinned at 3 for v0 (pool of 1 ⇒ rank-1
        always; pool of 2 ⇒ rank-1 or rank-2; pool of 3+ ⇒ rank-1..3).
     e. If no rows survive (b), reject with reason
        `missing_corridor_in_interval` + interval index.

3. Validate chain continuity:
     For each consecutive pair of chosen corridors (C_i, C_{i+1}):
       assert C_i's last cell is adjacent to C_{i+1}'s first cell,
       OR they share an anchor block (the CP block itself).
     If not, reject with reason `chain_broken` + interval index.

4. Return: (chosen_corridors, expected_time_ms, ai_confidence)
     expected_time_ms = sum of per-corridor expected times:
       per_corridor_ms = path_length_cells * BLOCK_SIZE_M / SPEED_PRIOR_M_S * 1000
       (same constants used by time_envelope labels — keep them one
        source of truth)
     ai_confidence = mean(learned_corridor_score across chosen corridors)
```

### Determinism rules

- Same (map, corridors, model_hash, random_seed, TOP_K_CANDIDATES)
  tuple → same chosen route. The tuple is the full assembly-input
  fingerprint; run_id's sha covers it via the inputs block.
- Random seed **is** used in assembly (step 2d) for Level-1 mutation
  — within the top-K tie-break-ordered candidates per interval. Two
  seeds on the same corpus produce deterministically different routes
  when any interval has ≥2 scored candidates.
- TOP_K_CANDIDATES is pinned at 3 for v0; bumping it is a v0.1+
  decision with its own scope-revision.
- Tie-break within a rank is explicit (path_length ASC, corridor_id
  ASC) so different implementations can't drift on near-ties.
- Block-selection mutations (Level-2 strip-to-route, Level-3 corridor
  substitution) are out of v0 scope; they ship under their own
  scope-revision.

## Finishability semantics

### `route_verified`

`true` if and only if:

1. The map is Linked-CP.
2. Route assembly completed without any `reject_reason`.
3. `ai_confidence >= 0.30` (a minimum sanity floor — below this,
   the model isn't committing enough to back "yes, this route is
   finishable" even if the chain closed).

Otherwise `false`.

### `estimated_time_ms`

`int` — sum of per-corridor expected times, as defined in the
algorithm above. Uses the same `_BLOCK_SIZE_M` and
`_DEFAULT_SPEED_PRIOR_M_S` constants as `src/corridor/ranking/
time_envelope_labels.py`. **Don't redefine these constants** in the
generator — import from the existing module. Keeps physics
consistent between label-time and gate-time.

### `ai_confidence`

`float in [0, 1]` — `mean(learned_corridor_score)` across chosen
corridors. Simple, honest, defensible.

Deliberate non-choice: **v0 does not calibrate confidence** (no
Platt scaling, no isotonic regression, no quantile mapping). If
the learned scores are off-calibration, the reported confidence is
off-calibration too — that's a known v0 limitation documented here,
not a bug.

### `reject_reason`

String enum, set only when `route_verified=false`:

| Reason | When |
|---|---|
| `plain_cp_not_supported_v0` | Map is not Linked-CP |
| `missing_corridor_in_interval` | At least one interval has zero learned-scored corridors |
| `chain_broken` | Chosen corridors are not spatially continuous |
| `empty_corridors` | No route_corridors rows at all for this map |
| `confidence_below_floor` | Chain closed but ai_confidence < 0.30 |
| `unknown_block` | Generator emitted a block_name not in the catalogue |
| `invalid_schema` | Generated JSON fails schema validation |

### `gate_version`

Literal `"finishability-v0"`. Consumers can safely assume the
above enumeration is exhaustive for this version.

### Replay-ground-truth interaction

Per `project_replay_ground_truth_contract.md`:

- If a **replay exists** on this specific base map and closes a
  Spawn→Goal route that the finishability gate rejects, the gate
  still returns `route_verified=false`. Replays don't bypass the
  gate.
- What replays DO affect: the traversability classification that
  route enumeration uses upstream. If replays promote a transition
  to `observed_traversable` (after multi-replay confirmation), the
  next route enumeration build will include corridors using that
  transition, and the gate will then see those corridors as
  available.
- The gate's role is narrow: "given the current route_corridors
  table, does a chain close?" Yes or no. It doesn't second-guess
  the corridor data.

## Operator-facing fields (what the UI shows)

The dashboard's "generate" button (currently stubbed — per PR A)
will eventually display exactly these three things per generated map:

- **Route Verified**: Yes / No (from `route_verified`)
- **Estimated Time**: `mm:ss.xxx` (from `estimated_time_ms`)
- **AI Confidence**: `0.74` (from `ai_confidence`)

If `route_verified=false`, show the `reject_reason` inline so the
operator knows why.

No other fields are surfaced in v0. Anything else is an
implementation detail.

## Provenance + reproducibility

Every artifact records:

- `run_id` — deterministic sha over inputs (16-hex)
- `model_hash` — the exact ridge model that scored the corridors
  used (joinable with `model_metrics.model_hash` and
  `route_corridors.learned_score_model_hash`)
- `learned_score_version` — scheme tag string (e.g.
  `time_envelope_v2_weighted@0.1.0`)
- `config_hash` — resolved config sha (same as `stage_runs` uses)
- `code_version` — repo sha at generation time
- `classification_version` — traversability classification the
  corridor graph was built under

To reproduce a generated artifact exactly:
1. Check out the pinned `code_version`.
2. Restore the DB state at that snapshot (corridors must have the
   pinned `model_hash`).
3. Re-run the generator with the `inputs` block and the same
   `config_hash`.
4. `run_id` should match bit-for-bit.

## Implementation PR sequencing (post-C)

After this doc lands, the implementation sequence is:

- **PR D** — pure-function scaffold: `src/generation/` module with
  `assemble_route(map_id, conn)` + `run_finishability_gate(route)`.
  No CLI, no generator yet — just the algorithms this doc pins,
  with unit tests. Enables reuse by future generator PRs and the
  dashboard's real `generate-map` action.
- **PR E** — minimal generator: takes a base_map_id, emits a
  modified JSON (copy the base's blocks, run assembly + gate on
  it, emit the v0 schema). Answers "does the pipeline produce a
  JSON file that the gate validates?" — not "is the output good."
- **PR F** — wire into Flask: replace the `generate-map` action's
  stub with the real PR-E path. Dashboard's Route-Verified /
  Estimated Time / AI Confidence fields become live.
- **PR G+** — scratch (non-base) generation, style/difficulty
  semantics, regeneration loops. Each gets its own design-doc
  revision before implementation.

## Level-2: strip-to-route (schema v0.1)

This section extends scope-v0 with the first geometrically-meaningful
mutation shipped under the bumped `schema_version: "generation-v0.1"`.
v0 output is a full copy of the base map's blocks; v0.1 (with
`strip=true`) emits only the blocks the chosen route actually needs.

### What changes

- **`inputs.strip`** — new boolean field, `false` by default. When
  true, assembly runs as v0 (same learned-score selection,
  seed-driven top-K pick) but the emitted `map.blocks` is filtered
  down to the route's cells.
- **`map.stripped`** — mirrors `inputs.strip` in the output so
  readers don't have to cross-reference.
- **`map.strip_policy`** — enum: `none` / `halo_axis_1` /
  `halo_axis_1_plus_anchor_radius_3`. `halo_axis_1_plus_anchor_radius_3`
  is the default for `--strip` from PR L onward; `halo_axis_1` stays
  available for reproducibility / comparative runs.
- **`map.kept_block_count`** + **`map.base_block_count`** — diagnostic
  counts so the operator can see "we stripped 541 → N blocks."
- **`schema_version`** bumps to `generation-v0.1` *only* when stripping.
  Non-stripped runs stay on `generation-v0` for backwards compatibility.
- **`reject_reason`** enum gains `stripped_route_broken`.
- **`inputs`** participates in `run_id` — so `(seed=42, strip=false)`
  and `(seed=42, strip=true)` have distinct run_ids + GBX paths.

### Strip policy `halo_axis_1`

For every cell in every chosen corridor's `path_cells`:
1. Keep the cell itself.
2. Keep its 6 grid-axis neighbours (±x, ±y, ±z).

Anchor cells are kept unconditionally via `Anchor.cell` (multi-cell
CPs have cells the route didn't step on; the game still registers
them as the same waypoint, so dropping them would break in-game race
structure).

Free-placed blocks (NULL grid coords) and `BakedBlocks` (stadium
scenery) are untouched.

**Known limitation** — this policy drops structural geometry around
Spawn / CP / Finish blocks when those blocks form multi-cell
assemblies that the chosen route doesn't step through. Canonical
case: map 1212's `PlatformPlasticLoopOutStartCurve1` cluster — 5
blocks surrounding the Spawn form the start ramp; `halo_axis_1`
kept only the one on the route and dropped the other 4, leaving the
car to spawn above nothing. PR L adds
`halo_axis_1_plus_anchor_radius_3` to address this. The
`halo_axis_1` policy stays available for reproducibility /
comparative analysis but **isn't the recommended default for
in-game use**.

### Strip policy `halo_axis_1_plus_anchor_radius_3`

Everything `halo_axis_1` does, plus:

1. **Free-placed waypoints snap to grid** via the canonical TM2020
   block dimensions `(32 m × 8 m × 32 m)`. Snapped cells join the
   anchor set for preservation.
2. **Every cell within Chebyshev distance 3** of any anchor cell
   (grid-placed or snapped-from-free) is kept unconditionally — a
   7×7×7 cube (343 cells) per anchor.

Covers multi-block start-curve / finish-gate / CP-ramp assemblies
(radial span ≤ 3 cells in the corpus we've seen). Anchor cubes
overlap where anchors are near each other, so total cell counts
stay modest. `halo_axis_1` stays as a reproducibility-only option.

**Known limitation** — captures only anchor-proximal structural
geometry. Mid-route pillars / bases / vertical supports that sit
below or above the drivable surface but far from any anchor get
dropped, producing visible "floating road / missing pillar" gaps
in-game. Operator in-game testing of map 1212 surfaced this
pattern after PR L shipped — see #217.

### Strip policy `halo_axis_1_plus_anchor_radius_3_vext_3`

Everything `halo_axis_1_plus_anchor_radius_3` does, plus:

3. **Vertical extension per route path cell** — for every cell in
   every chosen corridor's `path_cells`, also keep the cells at
   `(x, y ± {1, 2, 3}, z)`. A ±3 column along the Y axis.

Captures support / pillar / base geometry directly beneath or above
drivable cells. `halo_axis_1_plus_anchor_radius_3` (PR L) stays as
a reproducibility-only option.

**Known limitation** — the horizontal route-cell halo is still
axis-only; wall / transition / slope blocks sitting at **XZ-diagonal
offsets** from a path cell (same Y, ±1 X *and* ±1 Z) get dropped.
Map-1212 diagnostic (PR #56) isolated 20 such drops at route cell
`(31, 13, 22)` — walls, `TiltTransition2UpLeft`, `Slope2Straight`.
Fix ships under `halo_xz_cheb_1_vext_3_plus_anchor_radius_3`.

### Strip policy `halo_xz_cheb_1_vext_3_plus_anchor_radius_3`

Same as `halo_axis_1_plus_anchor_radius_3_vext_3` **except** the
per-path-cell horizontal halo is upgraded from axis-1 (±X and ±Z
independently, 4 cells + centre) to **full 3×3 XZ at the cell's Y**
(all 8 surrounding cells in the horizontal plane, cheb ≤ 1 in XZ).

15 distinct cells per path cell. Anchor cube radius 3 unchanged.
Superseded by the prism policy below after in-game testing on
map 1212 showed `y±1` XZ-diagonal drops were still breaking
drivability. Kept as a reproducibility-only option.

### Strip policy `halo_prism_3x7x3_plus_anchor_radius_3` (default)

Per route path cell, keep the **full 3×7×3 prism** around it: the
3×3 XZ neighbourhood at every Y in the ±3 range. 63 distinct cells
per path cell. Subsumes `xz_cheb_1` + `vext_3` into one volume.

Concretely:
- `(x ± 1, y ± {0, 1, 2, 3}, z ± 1)` for the route cell `(x, y, z)`.

Anchor cube radius 3 unchanged. Free blocks + `BakedBlocks`
unchanged.

**Rationale**: operator in-game testing after PR #57 showed
`(31, 13, 22)` on map 1212 still losing 16 blocks at `y ± 1` from
the route cell (wall tiles, `TiltTransition2UpLeft`, `Slope2Straight`,
`BlueIceHill` custom blocks). Those sat at XZ-diagonal offsets
combined with `y ± 1`, which neither `xz_cheb_1` (same Y only) nor
`vext_3` (axial ±Y only) covered.

**Cost**: ~4× more cells per path cell vs the previous default.
Map 1212 jumps from ~316 kept to ~400-500 kept (exact depends on
route-cell density vs anchor-cube overlap).

**Known limitation — still not a real fix for multi-cell blocks.**
A prism halo is still cell-origin-based. `PlatformPlasticSlope2*`
blocks really do span multiple cells via their mesh; our single-
origin strip can't preserve them coherently without reading
`CGameCtnBlockInfo` from the GBX. If in-game testing after the
prism still shows "half-shown spherical shapes," GBX mesh
introspection is the honest next step.

### Reject path is preserved

If the stripped cell-set doesn't preserve the chosen route (tighter
future policies might drop path cells), the gate re-run sets
`route_verified=false` + `reject_reason=stripped_route_broken` +
`detail` pointing at the failing interval. The artifact and GBX are
**still written** — the diagnostic signal is the whole point.

### What this section does NOT decide

- **Block substitution** (replace route cells with alternate families)
  — deferred to scope-v0.2+.
- **Cross-map corridor splicing** — that's Level-3, a bigger scope
  revision.

## Finishability-proof metadata (source maps only)

PR M adds a ``map_finishability_proof`` table tracking per-source-map
evidence that a map is actually finishable in-game: author-set medal
times (from the GBX itself), world-record time (derived as
``MIN(finish_time_ms)`` across our clean replays), and a derived
``proof_source`` enum.

**Hard boundary**: this metadata is **evidence, never a bypass**.

- The generator's internal finishability gate
  (``src.generation.finishability.run_finishability_gate``) runs
  **mandatorily** on every generated map. ``route_verified`` in the
  artifact's ``finishability`` block is set **only** by that gate.
- No ``map_finishability_proof`` field is consulted by the gate. A
  base map with a world-record replay + gold medal time still
  produces ``route_verified=false`` if the assembler can't chain a
  corridor route through it.
- The Flask UI renders proof as a label on the Generated maps panel
  ("Author validated" / "Player validated" / "Internally verified")
  — presented alongside ``route_verified``, never replacing it.

**`proof_source` precedence** (strongest → weakest, derived at
write-time):

1. ``replay`` — at least one ``clean`` / ``usable_with_warnings``
   replay has a ``finish_time_ms``.
2. ``author_time`` — the GBX carries an AuthorTime.
3. ``world_record`` — any replay exists on the map but none were
   marked clean. Weaker than ``replay`` because we haven't verified
   it in-engine.
4. ``internal_route`` — only our corridor gate says so.
5. ``none`` — no evidence yet.

**Why a separate table**: keeps the provenance layer pluggable for
future signals (leaderboard snapshots, telemetry-derived evidence,
community-flagged uploads). `maps` is a hot row; widening it for
every new evidence type ages badly.

## What this doc does NOT decide

Explicit open questions that are **not** v0 decisions. Follow-up
design-doc revisions will resolve them:

- **How the generator picks block placements for a scratch map.**
  v0 implementation starts with "take a base map, selectively
  replace corridors with equivalent alternates." Scratch generation
  waits for v0.1+.
- **How style tags influence generation.** v0 uses them only as a
  corpus filter. Style transfer / blending is a separate project.
- **How difficulty is expressed quantitatively.** v0 has three
  discrete buckets (`easy` / `medium` / `hard`) that affect
  tie-breaks only. Continuous difficulty is v0.1+.
- **Calibrated confidence.** v0 uses raw mean learned score; v0.1+
  can introduce proper calibration.
- **Multi-route output.** v0 is one best route; "variations" wait
  for an interactive iteration layer.
- **Replay-telemetry integration beyond classification.** Per the
  contract, v0 uses existing classification only; direct telemetry-
  informed generation is a post-OpenPlanet phase.

## Anti-drift checklist

When reviewing a generation implementation PR, verify:

- [ ] Output JSON includes every field this doc lists.
- [ ] Route assembly follows the algorithm above, including the
      seed-driven pick-within-top-K rule (step 2d) and deterministic
      tie-breaks within each rank.
- [ ] Finishability gate returns one of the documented
      `reject_reason` values; no free-form strings.
- [ ] `estimated_time_ms` constants are imported from
      `src/corridor/ranking/time_envelope_labels.py` (not
      re-declared).
- [ ] `ai_confidence = mean(learned_corridor_score)`, not something
      else.
- [ ] Linked-CP detection uses `tag = 'LinkedCheckpoint'` (not
      `tag = 'Checkpoint' AND waypoint_order >= 1`); plain-CP maps
      short-circuit with `plain_cp_not_supported_v0`.
- [ ] Multi-cell CPs are deduped by `(tag, waypoint_order)` when
      building the anchor chain — both in the enumerator's
      `_plan_intervals` and the assembler's `_detect_and_order_anchors`.
- [ ] Level-2 strip (if present): `schema_version = "generation-v0.1"`,
      `map.stripped = true`, `map.strip_policy ∈ {"halo_axis_1",
      "halo_axis_1_plus_anchor_radius_3",
      "halo_axis_1_plus_anchor_radius_3_vext_3",
      "halo_xz_cheb_1_vext_3_plus_anchor_radius_3",
      "halo_prism_3x7x3_plus_anchor_radius_3"}`,
      `map.kept_block_count` matches `len(map.blocks)`, anchor cells
      kept even when not on the chosen path. Artifact + GBX are
      written even when `reject_reason = "stripped_route_broken"`.
      Default policy for `--strip` is
      `halo_prism_3x7x3_plus_anchor_radius_3` from #217-c onward;
      earlier policies stay available for reproducibility.
- [ ] Provenance block is complete.
- [ ] No field surfaced in the JSON artifact is computed from
      data that could drift (e.g. no "map quality" score computed
      from the generator's own output).
