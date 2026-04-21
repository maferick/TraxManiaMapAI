-- TM2020 custom block names occasionally exceed 128 chars (observed on
-- the 1000-map scale test, snapshot 2026-04-scale-1k). Widen the
-- column so the parse stage doesn't reject a whole map for one
-- long-named custom block. block_family stays at 64 — the heuristic
-- extracts the leading CamelCase word, which is short.

ALTER TABLE block_placements
    MODIFY COLUMN block_type VARCHAR(255) NOT NULL;
