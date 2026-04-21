# Benchmark Policy

## Principle

Benchmark sets are frozen, versioned, and immutable. They are the fixed
yardstick against which evaluators and generators are measured. Editing
a benchmark in place is forbidden. The immutability is the whole point:
a moving yardstick measures nothing.

## Versioning rules

- Every benchmark set has a version, formatted as `<id>-v<N>` where
  `<N>` is a monotonic integer (1, 2, 3…). Example: `tech-strong-v1`,
  `tech-strong-v2`.
- Once released, a version is immutable. Fixing a mistake means
  publishing `tech-strong-v2`, not editing `tech-strong-v1`.
- Manifest files live in git under `data/benchmarks/<id>/<id>-v<N>.yaml`.
  The manifest is metadata only — raw map/replay data stays out of git.
- The raw data referenced by a manifest is pinned by content hash. At
  load time (PR 3 onwards) the loader verifies each hash against the
  artifact on disk.

Benchmark versions use monotonic integers, not semver, because a
benchmark set either changes or it doesn't — there is no meaningful
"patch" to a frozen set. Semver is used for evaluators and surrogates
(see `architecture.md` and `surrogate-policy.md`), not benchmarks.

## Categories (Phase 1 seed list)

Benchmark manifests must exist for, at minimum, the following
categories. Initial releases may use placeholder fixtures where real
data is not yet ingested; the manifest still gets a version.

1. **strong tech tracks** — hand-curated, known good.
2. **mediocre tech tracks** — hand-curated, known median.
3. **off-style tracks** — tracks that fail style classification by
   construction (fullspeed tagged as tech, etc.).
4. **alternate-line tracks** — tracks with known legitimate multiple
   racing lines; used for route-inference branch-recall evaluation.
5. **shortcut / skip tracks** — tracks with known skips; used to
   confirm the structural validator doesn't treat every skip as an
   error.
6. **rare-but-valid transition tracks** — tracks containing
   block-to-block transitions that are uncommon but legitimate; used
   for constraint-graph evidence fields.
7. **structurally broken tracks** (if obtainable) — unreachable
   blocks, missing finish, etc. Used for structural-validator
   true-positive rate.
8. **replay-noisy tracks** — tracks with pathological replays
   (teleports, timing glitches, spectator artifacts); used for
   replay-cleaning rejection evaluation.

The categories are not exhaustive — later phases will add more — but
the seed list exists so that every Phase 1 subsystem has at least one
benchmark it is evaluated against.

## Manifest schema

A benchmark manifest is a YAML file. The canonical schema lives at
`data/benchmarks/benchmark-manifest.schema.json` (JSON Schema, draft
2020-12). Fields:

### Required

| Field                  | Type                     | Notes                                                              |
|------------------------|--------------------------|--------------------------------------------------------------------|
| `schema_version`       | integer                  | Schema version of the manifest format itself. Current: `1`.        |
| `benchmark_id`         | string                   | Stable id, e.g. `tech-strong`. Must match filename stem prefix.    |
| `version`              | integer                  | Monotonic, starting at 1. Changes require a new manifest file.     |
| `category`             | enum (see seed list)     | One of the Phase 1 seed categories.                                |
| `ingestion_snapshot`   | string                   | Pinned snapshot id, e.g. `2026-04-tmx`.                            |
| `released_at`          | ISO 8601 date (YYYY-MM-DD)| The date this version was frozen.                                  |
| `author`               | string                   | Email or handle of the person who released this version.           |
| `entries`              | list of entries          | See entry schema below. Must be non-empty.                         |
| `rationale`            | string (≥ 20 chars)      | Why this benchmark exists and what it measures.                    |

### Optional

| Field                  | Type                     | Notes                                                              |
|------------------------|--------------------------|--------------------------------------------------------------------|
| `supersedes`           | string                   | e.g. `tech-strong-v1`. Present when this version replaces another. |
| `notes`                | string                   | Free-form release notes.                                           |
| `tags`                 | list of strings          | Extra labels. Must not encode validity or score.                   |

### Entry schema (each item in `entries`)

| Field                  | Type       | Notes                                                                        |
|------------------------|------------|------------------------------------------------------------------------------|
| `map_id`               | string     | Canonical map id from the ingestion snapshot.                                |
| `content_hash`         | string     | SHA-256 of the referenced artifact. Verified at load time.                   |
| `role`                 | enum       | One of `primary`, `reference`, `negative`. See below.                        |
| `label`                | object     | Hand-curated labels — not TMX tags. Schema is category-specific.             |
| `comment`              | string     | Optional per-entry note. Kept in-manifest, not in a separate sidecar file.   |

`role` distinguishes how an entry is used during evaluation:

- `primary` — the entry is part of the set being measured.
- `reference` — the entry is a known-good anchor (used for
  rank-correlation baselines). A manifest may have 0 reference
  entries but if it has any, they are called out.
- `negative` — the entry is a known-bad example (broken, off-style).
  The evaluator should fail or flag these; a benchmark without any
  negative entries risks a grader that only sees easy cases.

### What a manifest must NOT contain

- raw map binaries, replay binaries, or any multi-megabyte blobs
- self-reported TMX tags used as ground-truth labels
- references to an ingestion snapshot that does not exist in the
  canonical snapshots list
- a `version` that already exists on disk for the same `benchmark_id`

## Snapshot coupling

Every benchmark manifest references a specific ingestion snapshot.
TMX is a moving target (maps get added, renamed, deleted, reuploaded
with different ids), so a benchmark that does not pin a snapshot is
not reproducible.

Worked example:

- Ingestion run `2026-04-tmx` downloads 47,312 maps, produces canonical
  rows with `ingestion_snapshot = "2026-04-tmx"`.
- Benchmark `tech-strong-v1` pins `ingestion_snapshot: 2026-04-tmx`
  and references 50 map ids that exist in that snapshot.
- Three months later, `tech-strong-v2` is released. It pins snapshot
  `2026-07-tmx`. Of its 50 entries, 48 are the same `map_id` values
  as v1 (the map upload id didn't change), 1 was reuploaded with a
  new id, 1 was deleted upstream and replaced with a different map.
- v1 remains loadable against the `2026-04-tmx` snapshot. v2 is
  loadable against either snapshot — it will resolve v1's 48 shared
  maps from whichever snapshot is active, but only v2's snapshot
  guarantees all 50 entries resolve.

## Policy on human-labeled benchmarks

Tag noise from TMX is expected and accepted at the ingestion layer.
Benchmarks that require style or quality labels (e.g. `tech-strong`
vs `tech-mediocre`) must use **hand-curated labels**, not
self-reported TMX tags. This is load-bearing: the style classifier is
evaluated against benchmarks, so if benchmarks inherit tag noise the
classifier is being graded against its own training signal.

The hand-curated label set is versioned alongside the manifest — a
relabel is a new benchmark version.

## Release process

1. **Draft.** Create `data/benchmarks/<id>/<id>-v<N>.yaml` on a
   feature branch. `released_at` is left blank in the draft.
2. **Review.** At least one reviewer other than the author. Review
   checks: rationale is concrete, entries exist in the pinned
   snapshot, content hashes verify, no TMX-tag labels.
3. **Freeze.** On merge, set `released_at` to the merge date. After
   merge the file is immutable. A follow-up PR that edits a released
   manifest must be rejected.
4. **Announce.** Add an entry to the changelog in
   `data/benchmarks/CHANGELOG.md` (created when the first benchmark
   ships; absent until then).

The review step is mandatory even for placeholder benchmarks that
reference fixtures instead of real ingested maps. The point of the
review is to enforce schema + rationale quality, not the size of the
set.

## Immutability enforcement

Soft enforcement (Phase 1): the `BenchmarkManifest` loader verifies at
load time that the file on disk has not been modified relative to its
content hash recorded in git.

Hard enforcement (later): a pre-commit hook that rejects any change
to a file matching `data/benchmarks/*/*-v*.yaml` whose git history
already contains a merged commit. Not implemented in PR 2 — but
called out here so that a future PR can wire it up without
re-deciding the policy.

## What is not a benchmark

- a single curated map list used for training
- a rolling "latest top 100" list
- anything that changes without a new version
- the ingestion snapshot itself (snapshots are inputs to benchmarks,
  not benchmarks)

Benchmarks measure; they do not train.
