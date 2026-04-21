# data/benchmarks

Benchmark **manifests** live here. Raw map / replay data does not.

Manifests are versioned and immutable once released. To fix a mistake,
publish a new version — never edit an existing one. See
[`docs/benchmark-policy.md`](../../docs/benchmark-policy.md) for the
governing rules, the seed category list, and the release process.

## Layout

```
data/benchmarks/
  benchmark-manifest.schema.json   # JSON Schema (draft 2020-12)
  TEMPLATE.yaml                    # copy when authoring a new manifest
  README.md                        # this file
  <benchmark_id>/
    <benchmark_id>-v1.yaml
    <benchmark_id>-v2.yaml
    ...
```

One directory per `benchmark_id`. Every file inside it is a frozen
version.

## Authoring a new manifest

1. Copy `TEMPLATE.yaml` to `<benchmark_id>/<benchmark_id>-v1.yaml`.
2. Fill in every required field. The `rationale` field must be
   substantive — review rejects generic or placeholder rationale.
3. Validate:

   ```
   python -m src.benchmarks validate <path>
   ```

4. Open a PR. At least one reviewer other than the author is required.
5. On merge the file becomes immutable. A later PR that edits the file
   must be rejected — publish a new version instead.

## Publishing a new version of an existing benchmark

1. Create `<benchmark_id>/<benchmark_id>-v<N+1>.yaml`.
2. Set `supersedes: <benchmark_id>-v<N>`.
3. Update `ingestion_snapshot` to the current pinned snapshot.
4. Review + merge as for a new manifest.

The previous version stays on disk and remains loadable. Reproducing
an older evaluation means loading the older manifest, not "diffing
against the latest".

## What is gitignored under this tree

Raw artifacts referenced by manifests do not live in git. The
`.gitignore` entry `data/benchmarks/raw/` reserves a location for
mirrored raw data on individual dev machines if needed; it is not
the source of truth.
