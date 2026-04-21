-- Canonical maps.
--
-- Primary key is surrogate; the natural key is
--   (source_system, source_map_id, ingestion_snapshot)
-- which is unique so reingestion under a new snapshot produces a new
-- row rather than overwriting.
--
-- parse_status / parse_error_code form the closed taxonomy the
-- ingestion + parser boundary reports against. See src/parsers/errors.py.

CREATE TABLE IF NOT EXISTS maps (
    id                    BIGINT        NOT NULL AUTO_INCREMENT,
    source_system         VARCHAR(32)   NOT NULL,
    source_map_id         VARCHAR(128)  NOT NULL,
    ingestion_snapshot    VARCHAR(64)   NOT NULL,

    title                 VARCHAR(255)  NULL,
    author                VARCHAR(255)  NULL,
    environment           VARCHAR(64)   NULL,
    style_tags_raw        JSON          NULL,
    length_estimate_ms    BIGINT        NULL,
    award_count           INT           NULL,
    average_rating        DECIMAL(4,2)  NULL,
    popularity_metric     BIGINT        NULL,
    has_items             TINYINT(1)    NOT NULL DEFAULT 0,
    is_block_mode         TINYINT(1)    NOT NULL DEFAULT 1,

    parser_version        VARCHAR(32)   NOT NULL,
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
    UNIQUE KEY uq_maps_natural (source_system, source_map_id, ingestion_snapshot),
    KEY ix_maps_snapshot_status (ingestion_snapshot, parse_status),
    KEY ix_maps_hash (raw_artifact_hash),

    CONSTRAINT fk_maps_snapshot
        FOREIGN KEY (ingestion_snapshot)
        REFERENCES ingestion_snapshots (snapshot_id)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;
