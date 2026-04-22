-- Persist learned corridor score alongside the heuristic
-- corridor_confidence. The two coexist by design — the heuristic is
-- hand-tuned and interpretable; the learned score is model-derived
-- and may be stronger. Keeping both lets the dry-run surface them
-- side by side for comparison, and downgrades the risk of swapping
-- out the heuristic before the learned path proves out.
--
-- learned_corridor_score is the raw model prediction (unclipped).
-- Ridge regression can produce values outside [0, 1] on feature
-- combinations the training set didn't cover well; consumers clip at
-- display time if they need comparability with corridor_confidence.
--
-- Provenance:
--   learned_score_version  — the label-scheme tag at training time
--                            (e.g. "time_envelope@0.1.0").
--   learned_score_model_hash — sha256 of the weights+feature_names
--                              JSON, so a re-score pass can prove
--                              which model produced a given row.
--
-- NULL means "not scored yet" — score-corridors-learned fills it in
-- after train-corridor-ranking has emitted a model JSON.

ALTER TABLE route_corridors
    ADD COLUMN learned_corridor_score DOUBLE NULL AFTER score_version,
    ADD COLUMN learned_score_version VARCHAR(64) NULL AFTER learned_corridor_score,
    ADD COLUMN learned_score_model_hash CHAR(64) NULL AFTER learned_score_version,
    ADD KEY ix_rc_learned_score (map_id, learned_corridor_score);
