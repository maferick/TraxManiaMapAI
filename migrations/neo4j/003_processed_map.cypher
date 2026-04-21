// ProcessedMap nodes are the pipeline's idempotency ledger: one node
// per (map_id, snapshot_id, parser_version) combination that has
// contributed observations to the graph. The pipeline's MERGE on this
// node detects first-time processing vs re-runs.

CREATE CONSTRAINT processed_map_identity IF NOT EXISTS
FOR (p:ProcessedMap) REQUIRE (p.map_id, p.snapshot_id, p.parser_version) IS UNIQUE;
