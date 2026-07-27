-- Rename: these are CANDIDATE jumps, not observed ones.
--
-- The table was populated from the corpus-finishable axiom — every
-- published, parsing map can be driven, so a gap its racing line must
-- cross is drivable. That reasoning has a hole: the axiom licenses
-- only the gaps the successful run ACTUALLY crossed. An open-end pair
-- in a finishable map may equally be scenery, an unused alternative
-- route, a shortcut, or two parallel sections that never connect.
--
-- So the rows are candidates. Replay extraction promotes one to
-- `observed` when an inferred driven sequence crosses it, and at that
-- point the row can also carry entry speed, direction, airtime and
-- landing state — far more useful than a binary "a gap appears in a
-- finishable map".

RENAME TABLE block_jump_pairs TO block_candidate_jump_pairs;

ALTER TABLE block_candidate_jump_pairs
    ADD COLUMN evidence VARCHAR(16) NOT NULL DEFAULT 'candidate'
        COMMENT 'candidate = open-end pair in a finishable map; observed = a replay crossed it',
    ADD KEY idx_jump_evidence (evidence, map_count);
