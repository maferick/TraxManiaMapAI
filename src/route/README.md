# src/route

Route inference scaffold (PR 5).

## Layout

| File / dir               | Role                                                              |
|--------------------------|-------------------------------------------------------------------|
| `artifact.py`            | On-disk JSON shape: `Centerline`, `BranchCandidate`, `SegmentBoundary`, serialization + content-hash. |
| `projection.py`          | Vectorized projection of telemetry onto a centerline polyline; seed-centerline builder. |
| `clusterers/base.py`     | `Clusterer` ABC, `ClusterResult`, registry (`create`, `get`, `register`). |
| `clusterers/grid.py`     | `GridClusterer` — default, pure numpy, deterministic.            |
| `clusterers/dbscan.py`   | `DbscanClusterer` — lazy sklearn adapter (install `[learn]` extra). |
| `clusterers/per_segment.py` | `PerSegmentClusterer` — wraps any inner clusterer in sliding s-windows. |
| `extract.py`             | `RouteExtractor` — seed → refine → cluster → branches/segments. |
| `pipeline.py`            | `RoutePipeline` — DB orchestrator, writes artifact file + row.   |

## Pluggability rule (from CLAUDE.md)

Clustering **must not be hardwired**. The `Clusterer` ABC + registry
gates new clusterers behind a single entry point (`route.create(name,
params)`). Concrete clusterers are selected by config — not by
imports in the extractor.

The three shipped clusterers demonstrate the seam:

- `grid` — cheap, deterministic, no ML deps. Default for scaffold.
- `dbscan` — lazy-imports `sklearn.cluster.DBSCAN`. Install the
  optional `[learn]` extra to use it.
- `per_segment` — composite that applies any inner clusterer inside
  sliding windows along an ordering coordinate (column 0 of the
  input array, typically arc-length).

## Running

After `python -m src.cli migrate` + PR 3 ingestion + PR 4 replay
cleaning + cohort assignment, route artifacts are extracted with:

```bash
python -m src.cli extract-route \
    --snapshot 2026-04-tmx \
    --map-id 42 \
    --clusterer grid
```

With no `--map-id`, the pipeline walks every map in the snapshot that
has at least `min_replays_per_map` cohort-assigned replays. Re-runs
with the same `route_version` skip existing rows; bump the version to
re-extract.

## Telemetry contract

The extractor consumes `ReplayTelemetry` (see
`src/replay/telemetry.py`). Loading is pluggable via
`TelemetryLoader`; the default `FileTelemetryLoader` reads
`<raw_artifact_path>.telemetry.json` sidecars that the GBX wrapper
(external, not yet built) will emit.

## Not in scope for PR 5

- production-quality branch pruning (the current extractor flags
  branch candidates by cluster multiplicity; a quality score comes
  later alongside human-labeled fixtures)
- multi-segment route comparison (PR 6+)
- segment-type classification (corner vs straight; later phase)
- a real HDBSCAN adapter (the abstraction supports adding one; the
  DBSCAN adapter pattern is the template)
