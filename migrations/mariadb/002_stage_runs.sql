-- Pipeline stage provenance. Every resumable / idempotent pipeline
-- stage emits one row per invocation. CLAUDE.md requires:
--   inputs, outputs, resolved config hash, code version, duration.

CREATE TABLE IF NOT EXISTS stage_runs (
    id                   BIGINT        NOT NULL AUTO_INCREMENT,
    stage                VARCHAR(64)   NOT NULL,
    stage_version        VARCHAR(32)   NOT NULL,
    started_at           DATETIME(6)   NOT NULL,
    completed_at         DATETIME(6)   NULL,
    duration_ms          BIGINT        NULL,
    resolved_config_hash CHAR(64)      NOT NULL,
    code_version         VARCHAR(64)   NOT NULL,
    input_ref            VARCHAR(255)  NOT NULL,
    output_summary       JSON          NULL,
    status               ENUM('running','success','partial','failed') NOT NULL,
    error_taxonomy_code  VARCHAR(64)   NULL,
    error_message        TEXT          NULL,
    PRIMARY KEY (id),
    KEY ix_stage_runs_stage_started (stage, started_at),
    KEY ix_stage_runs_status (status, started_at)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;
