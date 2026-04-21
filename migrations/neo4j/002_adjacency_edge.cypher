// Adjacency-edge metadata indices. Neo4j 5 supports indexes on
// relationship properties — these are not constraints because an
// edge IS the (a)-[:ADJACENT_TO]->(b) identity, which is enforced
// by the pipeline (one edge per ordered pair via MERGE).

CREATE INDEX adjacency_validity IF NOT EXISTS
FOR ()-[r:ADJACENT_TO]-() ON (r.validity_label);

CREATE INDEX adjacency_last_snapshot IF NOT EXISTS
FOR ()-[r:ADJACENT_TO]-() ON (r.last_seen_snapshot);
