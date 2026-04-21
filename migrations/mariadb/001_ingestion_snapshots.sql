-- Ingestion snapshots. Each ingestion run pins a snapshot id; every
-- downstream row that references "which world" it was ingested from
-- joins to this table.

CREATE TABLE IF NOT EXISTS ingestion_snapshots (
    snapshot_id         VARCHAR(64)   NOT NULL,
    source_system       VARCHAR(32)   NOT NULL,
    started_at          DATETIME(6)   NOT NULL,
    completed_at        DATETIME(6)   NULL,
    user_agent          VARCHAR(255)  NOT NULL,
    rate_limit_rps      DECIMAL(8,3)  NOT NULL,
    resolved_config_hash CHAR(64)     NOT NULL,
    code_version        VARCHAR(64)   NOT NULL,
    notes               TEXT          NULL,
    PRIMARY KEY (snapshot_id),
    KEY ix_ingestion_snapshots_source (source_system, started_at)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;
