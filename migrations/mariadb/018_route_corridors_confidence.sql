-- Materialize corridor confidence on route_corridors. Score combines
-- the four evidence signals (rule_support, path_support_count,
-- pattern_weight, negative_evidence_count) across every edge in the
-- corridor path into a single [0, 1] confidence number. Design:
--   corridor_confidence = min(per-edge confidence) × virtual-edge factor
--
-- The formula lives in src/corridor/scoring.py; this column just
-- persists the result so the evaluator doesn't re-score on every
-- query. NULL means "not scored yet" — scoring is populated by a
-- separate `score-route-corridors` CLI pass, not at corridor-build
-- time (keeps concerns separate: enumeration is Step 4, scoring is
-- a later layer).

ALTER TABLE route_corridors
    ADD COLUMN corridor_confidence DOUBLE NULL AFTER contains_virtual_edge,
    ADD COLUMN score_version VARCHAR(32) NULL AFTER corridor_confidence,
    ADD KEY ix_rc_confidence (map_id, corridor_confidence);
