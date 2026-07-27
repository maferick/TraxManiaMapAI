-- Baked blocks as a fourth candidate source in coverage accounting.
--
-- Kept as its own column rather than folded into covered_grid so the
-- contribution of each matching path stays measurable. That matters
-- here specifically: baked blocks were added because of one diagnosed
-- 87 m gap, and the claim that they fix it has to be checkable by
-- ablation rather than taken on faith.

ALTER TABLE replay_telemetry_coverage
    ADD COLUMN IF NOT EXISTS covered_baked INT NOT NULL DEFAULT 0
        AFTER covered_item;
