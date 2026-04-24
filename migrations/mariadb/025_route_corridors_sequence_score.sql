-- Phase 2 #218-5 — combined_sequence_score on route_corridors.
--
-- Per-corridor composite of #218-4's pattern+geometry score
-- (averaged over the corridor's consecutive cell pairs). Used by
-- the generator's assembly as a tier-below tie-break after
-- learned_corridor_score. Soft signal per #218 scope; never
-- overrides traversability.

ALTER TABLE route_corridors
    ADD COLUMN combined_sequence_score FLOAT NULL
        COMMENT 'Avg of sequence_score.combined across path cell pairs';
