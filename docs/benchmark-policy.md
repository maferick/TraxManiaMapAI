# Benchmark Policy

**Status:** stub. Full content lands in PR 2.

## Principle

Benchmark sets are frozen, versioned, and immutable. They are the fixed
yardstick against which evaluators and generators are measured. Editing a
benchmark in place is forbidden.

## Versioning rules

- Every benchmark set has a version, e.g. `tech-strong-v1`.
- Once released, a version is immutable. Fixing a mistake means publishing
  `tech-strong-v2`, not editing `v1`.
- Manifest files live in git under `data/benchmarks/` (metadata only — not
  raw map data).
- The raw data referenced by a manifest is pinned by content hash.

## Categories (Phase 1 seed list)

Benchmark metadata manifests must exist for, at minimum:

1. strong tech tracks
2. mediocre tech tracks
3. off-style tracks
4. alternate-line tracks
5. shortcut / skip tracks
6. rare-but-valid transition tracks
7. structurally broken tracks (if obtainable)
8. replay-noisy tracks

Initial releases may use placeholder fixtures where real data is not yet
ingested; the manifest still gets a version.

## Manifest schema

To be defined in PR 2. At minimum a manifest records:

- benchmark id + version
- category
- ingestion snapshot version referenced
- list of map ids + content hashes
- release date
- notes / rationale
- author

## Snapshot coupling

Every benchmark manifest references a specific ingestion snapshot (TMX is a
moving target — maps get added, renamed, deleted). Reproducibility requires
the benchmark to pin its snapshot.

## Policy on human-labeled benchmarks

Tag noise from TMX is expected. Benchmarks that require style or quality
labels (e.g. `strong tech` vs `mediocre tech`) must use hand-curated labels,
not self-reported TMX tags.

## What is not a benchmark

- a single curated map list used for training
- a rolling "latest top 100" list
- anything that changes without a new version

Benchmarks measure; they do not train.
