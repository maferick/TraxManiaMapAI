# src/route

Route inference scaffold. Not implemented yet. Lands in PR 5.

## Key rule

Clustering must be **pluggable**. Do not hardcode DBSCAN anywhere. The
abstraction must support at least:

- DBSCAN
- HDBSCAN
- per-segment clustering
- future alternatives without touching call sites

Clustering parameters come from config, never from in-code constants.
