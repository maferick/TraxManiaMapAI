-- Derived replay features. Raw telemetry samples do NOT live here —
-- they live on the filesystem referenced by raw_artifact_path on the
-- replays row. This table holds the DB-resident derived representation.

CREATE TABLE IF NOT EXISTS replay_features (
    id                        BIGINT        NOT NULL AUTO_INCREMENT,
    replay_id                 BIGINT        NOT NULL,
    feature_extractor_version VARCHAR(32)   NOT NULL,

    features                  JSON          NOT NULL,
    diagnostics               JSON          NULL,

    created_at                DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    created_by_version        VARCHAR(32)   NOT NULL,
    source_artifact_ids       JSON          NOT NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uq_replay_features (replay_id, feature_extractor_version),
    KEY ix_replay_features_extractor (feature_extractor_version),

    CONSTRAINT fk_replay_features_replay
        FOREIGN KEY (replay_id) REFERENCES replays (id)
        ON DELETE CASCADE
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;
