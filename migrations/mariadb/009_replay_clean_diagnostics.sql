-- Per-replay cleaning diagnostics: rule-by-rule evidence stored as JSON.
-- The stage_run row carries the batch summary; this column carries the
-- granular per-replay detail the pipeline wrote when it classified.

ALTER TABLE replays
    ADD COLUMN clean_diagnostics JSON NULL AFTER cohort_membership;
