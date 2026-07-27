-- Rename: these are CANDIDATE jumps, not observed ones.
--
-- The table was populated from the corpus-finishable axiom — every
-- published, parsing map can be driven, so a gap its racing line must
-- cross is drivable. That reasoning has a hole: the axiom licenses
-- only the gaps the successful run ACTUALLY crossed. An open-end pair
-- in a finishable map may equally be scenery, an unused alternative
-- route, a shortcut, or two parallel sections that never connect.
--
-- Replay extraction promotes a candidate to observed, and at that
-- point the row can also carry entry speed, direction, airtime and
-- landing state.
--
-- NOTE both statements are guarded. DDL is not transactional in
-- MariaDB, so the first attempt at this migration renamed the table
-- and then failed on the ALTER, leaving the schema half-moved with
-- nothing recorded as applied. (The ALTER failed because the runner
-- splits on semicolons and the COMMENT text contained one.)

RENAME TABLE IF EXISTS block_jump_pairs TO block_candidate_jump_pairs;

ALTER TABLE block_candidate_jump_pairs
    ADD COLUMN IF NOT EXISTS evidence VARCHAR(16) NOT NULL DEFAULT 'candidate',
    ADD KEY IF NOT EXISTS idx_jump_evidence (evidence, map_count);
