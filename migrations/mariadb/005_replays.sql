-- Canonical replays.
--
-- Natural key: (source_system, source_replay_id, ingestion_snapshot).
-- clean_status is assigned by the replay-cleaning stage (PR 4);
-- ingestion writes 'unprocessed'.

CREATE TABLE IF NOT EXISTS replays (
    id                    BIGINT        NOT NULL AUTO_INCREMENT,
    source_system         VARCHAR(32)   NOT NULL,
    source_replay_id      VARCHAR(128)  NOT NULL,
    map_id                BIGINT        NOT NULL,
    ingestion_snapshot    VARCHAR(64)   NOT NULL,

    player_login          VARCHAR(128)  NULL,
    player_display_name   VARCHAR(255)  NULL,
    finish_time_ms        BIGINT        NULL,
    rank_metadata         JSON          NULL,

    clean_status          ENUM('unprocessed','clean','usable_with_warnings','rejected')
                                        NOT NULL DEFAULT 'unprocessed',
    clean_version         VARCHAR(32)   NULL,
    cohort_membership     JSON          NULL,

    parse_status          ENUM('unparsed','success','failed_transient','failed_permanent','skipped')
                                        NOT NULL DEFAULT 'unparsed',
    parse_error_code      VARCHAR(64)   NULL,
    parse_error_detail    TEXT          NULL,

    raw_artifact_path     VARCHAR(512)  NULL,
    raw_artifact_hash     CHAR(64)      NULL,

    created_at            DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at            DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                        ON UPDATE CURRENT_TIMESTAMP(6),
    created_by_version    VARCHAR(32)   NOT NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uq_replays_natural (source_system, source_replay_id, ingestion_snapshot),
    KEY ix_replays_map (map_id),
    KEY ix_replays_clean_status (clean_status, created_at),
    KEY ix_replays_hash (raw_artifact_hash),

    CONSTRAINT fk_replays_map
        FOREIGN KEY (map_id) REFERENCES maps (id),
    CONSTRAINT fk_replays_snapshot
        FOREIGN KEY (ingestion_snapshot) REFERENCES ingestion_snapshots (snapshot_id)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;
