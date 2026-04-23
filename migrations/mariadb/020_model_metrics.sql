-- Phase 2 PR B: persisted model-metrics history.
--
-- Every training run emits one row per label-scheme into this table.
-- The dashboard reads the most-recent row (per scheme) to show
-- "current" AI Quality / Variety scores, and the last N rows to
-- compute a trend (improving / flat / worsening).
--
-- Design choices:
--
-- - One row per (run_id, scheme). run_id groups schemes from one
--   training invocation so "compare inverse_rank vs v2_weighted at
--   α=1.0 on this run" is a trivial query.
-- - model_hash is sha256 of the model weights (same hash that
--   score-corridors-learned stamps on route_corridors rows). Joining
--   this table with route_corridors.learned_score_model_hash proves
--   which training run produced the currently-deployed scores.
-- - Nullable score columns — not every scheme produces an AUC, so
--   AUC is NULL-allowed. ai_quality_score and variety_score are
--   derived synthetic scores; they can be NULL when inputs are
--   missing (e.g. no diversity report at record time).
-- - No foreign keys to route_corridors. The learned-score column
--   on route_corridors is owned by scoring runs; this table is
--   owned by training runs. They converge on model_hash but don't
--   lock each other's cleanup.

CREATE TABLE model_metrics (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    run_id VARCHAR(32) NOT NULL,
    recorded_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    model_hash CHAR(64) NOT NULL,
    scheme VARCHAR(64) NOT NULL,
    alpha DOUBLE NOT NULL,
    n_labeled INT NOT NULL,
    train_rmse DOUBLE NULL,
    test_rmse DOUBLE NULL,
    test_rank_corr DOUBLE NULL,
    heuristic_rank_corr DOUBLE NULL,
    pred_stdev DOUBLE NULL,
    heuristic_stdev DOUBLE NULL,
    pred_stdev_ratio DOUBLE NULL,
    auc_learned DOUBLE NULL,
    auc_heuristic DOUBLE NULL,
    auc_delta DOUBLE NULL,
    diversity_delta_median DOUBLE NULL,
    diversity_delta_mean DOUBLE NULL,
    ai_quality_score DOUBLE NULL,
    variety_score DOUBLE NULL,
    snapshot_filter VARCHAR(64) NULL,
    code_version VARCHAR(64) NOT NULL,
    config_hash CHAR(64) NOT NULL,

    UNIQUE KEY uq_model_metrics_run_scheme (run_id, scheme),
    KEY ix_model_metrics_recorded_at (recorded_at),
    KEY ix_model_metrics_scheme_recent (scheme, recorded_at),
    KEY ix_model_metrics_model_hash (model_hash)
);
