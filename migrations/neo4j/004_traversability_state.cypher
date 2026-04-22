// Traversability label index on ADJACENT_TO edges.
//
// Edge labeling is family-classification driven (see
// src/corridor/traversability/classification.py). Every edge gets
// one of: "seed_valid", "unsupported", "unknown". The index makes
// the "traversability subgraph" view — filter by
// r.traversability_state = 'seed_valid' — constant-time at query
// time rather than a full scan.
//
// The CREATE statement is idempotent (IF NOT EXISTS); re-running
// this migration is a no-op. Downstream queries must tolerate edges
// without the property set (legitimately missing if the edge was
// observed after the most recent labeling pass — the labeling CLI
// is eventually-consistent by design, not transactional with
// adjacency extraction).

CREATE INDEX adjacency_traversability_state IF NOT EXISTS
FOR ()-[r:ADJACENT_TO]-() ON (r.traversability_state);

CREATE INDEX adjacency_traversability_labeled_at IF NOT EXISTS
FOR ()-[r:ADJACENT_TO]-() ON (r.traversability_labeled_at);
