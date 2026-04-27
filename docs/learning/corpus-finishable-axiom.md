# Corpus-finishable axiom

**Status:** active — adopted 2026-04-27
**Owner:** generation + learning
**Sibling docs:** `docs/generation/generation-scope-v0.md`,
`docs/learning/replay-coverage-expansion-plan.md`

## The axiom

> Every map ingested into our corpus is finishable.

A map enters the corpus only after it has been:

1. **Published** to TMX (the platform requires the author to upload a
   parseable `.Map.Gbx`).
2. **Downloaded** by our ingestion pipeline (a network 200 + a non-zero
   payload).
3. **Parsed** by our GBX wrapper without a hard error (`parse_status =
   'ok'`).

Each of those gates is independent. By the time a row appears in
`maps`, we have evidence that *somebody else's TM2020 install opened
the file and drove past the spawn block*. That's not the same level of
evidence as a clean replay, but it is strictly stronger than what we
were treating it as: nothing.

## Why this changed

Before this doc the stack treated maps without replays / author times /
internal-route gate output as **`proof_source = 'none'`** — visually
indistinguishable in dashboards from "we never saw this map." Several
downstream filters then over-corrected on the assumption that absent
positive evidence implied unsafe data:

* `_MIN_MAP_COUNT_THRESHOLD = 20` in `ai_generator.py` — drops any
  block appearing in fewer than 20 corpus maps.
* `'corpus_published' missing from proof_source enum` — the renderer's
  badge for "we have the map and nothing else" was the same as "we
  have nothing."
* `block_pair_transitions` is replay-driven only — pair counts are
  computed from `route_corridors.path_cells`, never from raw map block
  lists. Maps without replays contribute zero transition signal even
  though their block-list adjacencies are by-construction "this pair
  appears in a finishable map."

Each of those is fixable; the axiom names the unifying rationale so
the fixes don't read as ad-hoc.

## What changes in this PR

| change | location | effect |
|---|---|---|
| Add `corpus_published` tier | `migrations/mariadb/027_corpus_published_proof.sql` + `finishability_proof.py` | Default proof tier for ingested maps; renderer can show "Published map (axiom)" instead of nothing. |
| `_MIN_MAP_COUNT_THRESHOLD: 20 → 1` | `ai_generator.py` | A block appearing in even one corpus map is title-pack-safe within that pack; the `base_families` filter still scopes us to the right pack. |
| `_ALLOWED_SHAPES → {"straight"}` | `ai_generator.py` | Independent fix discovered in same investigation: the unit-cell walker can't honor curve/ramp/loop geometry. (Not directly axiom-driven; documented here because it landed in the same PR.) |

## What this PR does NOT do (queued)

These are the rest of the stack changes the axiom implies. Each is
its own PR-worth of work; listed in priority order.

### 1. Map-block-list co-occurrence priors

`block_pair_transitions` is replay-driven. Under the axiom, every
adjacent block pair in any corpus map is "this pair appears in a
finishable, loadable map" — a weaker but vastly higher-coverage
signal than replay-driven pairs. Build a sibling table
`block_pair_corpus_co_occurrence` keyed on `(map_id, family_a, name_a,
family_b, name_b)` aggregated over `block_placements` adjacencies.
Use it as a fallback prior when the replay-driven pair prior is zero.

**Cost:** one new pipeline stage (~few minutes on the corpus per the
4 GB host budget — must use SSCursor per `project_pipeline_memory_budget`).

### 2. Per-block placement evidence (geometry validity)

Every multi-cell block in a corpus map is at minimum *placeable* (the
file loaded). The current generator filters `footprint_x = 1` to dodge
the meshing problem; under the axiom we have direct evidence that
`(block, rotation, cell)` triples in corpus maps are valid placements,
which lets the multi-cell walker (M2 workstream) train against a
concrete placement table instead of inferring footprints from
geometry classifications alone.

### 3. Backfill `proof_source = corpus_published` on existing rows

The new tier's default in the migration covers new inserts. Existing
rows with `proof_source = 'none'` need a separate UPDATE pass to
re-derive against current evidence (most will move up to
`corpus_published`; some will move higher if author times / replays
landed after their last derivation).

```sql
UPDATE map_finishability_proof p
JOIN maps m ON m.id = p.map_id
SET p.proof_source = 'corpus_published',
    p.recorded_at = CURRENT_TIMESTAMP(6)
WHERE p.proof_source = 'none';
```

Defer to a maintenance window — touches the entire table.

### 4. Generator `connector_hint <> ''` filter relaxation

The catalogue currently drops blocks with empty `connector_hint`.
Under the axiom, a block with no classified hint that nonetheless
appears in a finishable map IS connectable in *some* configuration.
Replace the binary filter with a soft penalty, treating empty hint as
"unknown" rather than "unusable."

### 5. Dashboard surfacing

`corpus_published` deserves an explicit row in the proof-source
mix-by-environment widget. Today the dashboard shows a `none` slice
that should mostly migrate up.

## Boundaries

The axiom is a **labeling claim**, not a generation safety claim.

- The generator's finishability gate still runs mandatorily on every
  generated artifact. A base map being `corpus_published` is no
  reason to short-circuit `run_finishability_gate` on its derivatives.
- The `replay-ground-truth learning contract` (CLAUDE.md) still
  applies: only sustained multi-replay confirmation promotes a
  transition into `observed_traversable`. Map presence is not a
  replay.

If at some point the axiom's basis turns out to be flaky (e.g. a
particular ingestion source delivers a non-trivial fraction of
corrupt-but-parseable files), this doc gets revised + the catalogue
filters re-tightened, not the gate softened.
