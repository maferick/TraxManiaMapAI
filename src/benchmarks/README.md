# src/benchmarks

Benchmark manifest loader. Manifests are authored in YAML under
`data/benchmarks/` and validated against the JSON Schema at
`data/benchmarks/benchmark-manifest.schema.json`.

## Usage

From the repo root:

```bash
python -m src.benchmarks validate data/benchmarks/<id>/<id>-v1.yaml
```

In code:

```python
from pathlib import Path
from src.benchmarks import load

manifest = load(Path("data/benchmarks/example-benchmark/example-benchmark-v1.yaml"))
print(manifest.version_id, len(manifest.entries))
```

## What this module does NOT do

- verify content hashes against artifacts on disk (PR 3 — when
  ingestion produces the artifacts)
- guard against editing an already-released manifest (that is
  enforced by the git workflow — see `docs/benchmark-policy.md`)
- resolve upstream snapshot metadata (PR 3)

Only schema-level validation and the filename-stem check are in scope
for PR 2.
