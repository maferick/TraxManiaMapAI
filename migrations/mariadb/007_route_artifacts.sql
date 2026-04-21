-- Route inference artifacts (PR 5 produces these). Centerline data is
-- a filesystem reference — large arrays stay off the row.

CREATE TABLE IF NOT EXISTS route_artifacts (
    id                    BIGINT        NOT NULL AUTO_INCREMENT,
    map_id                BIGINT        NOT NULL,
    route_version         VARCHAR(32)   NOT NULL,

    centerline_path       VARCHAR(512)  NOT NULL,
    centerline_hash       CHAR(64)      NOT NULL,
    branches              JSON          NULL,
    segment_boundaries    JSON          NULL,

    clustering_method     VARCHAR(64)   NOT NULL,
    clustering_params     JSON          NOT NULL,
    replay_cohort         VARCHAR(32)   NOT NULL,

    extraction_confidence DECIMAL(5,4)  NULL,
    diagnostics           JSON          NULL,

    created_at            DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    created_by_version    VARCHAR(32)   NOT NULL,
    source_artifact_ids   JSON          NOT NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uq_route_artifacts (map_id, route_version),
    KEY ix_route_artifacts_method (clustering_method),

    CONSTRAINT fk_route_artifacts_map
        FOREIGN KEY (map_id) REFERENCES maps (id)
        ON DELETE CASCADE
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;
