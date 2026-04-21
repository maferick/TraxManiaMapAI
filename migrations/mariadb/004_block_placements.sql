-- Block placements on a map, keyed by parser_version so multiple
-- parser versions can coexist during transitions (CLAUDE.md rule:
-- "Multiple versions of derived artifacts may coexist during transitions").

CREATE TABLE IF NOT EXISTS block_placements (
    id                  BIGINT        NOT NULL AUTO_INCREMENT,
    map_id              BIGINT        NOT NULL,
    parser_version      VARCHAR(32)   NOT NULL,

    block_family        VARCHAR(64)   NOT NULL,
    block_type          VARCHAR(128)  NOT NULL,
    variant             VARCHAR(64)   NULL,

    x                   INT           NOT NULL,
    y                   INT           NOT NULL,
    z                   INT           NOT NULL,
    rotation            SMALLINT      NOT NULL DEFAULT 0,
    flags               INT           NULL,
    surface             VARCHAR(64)   NULL,

    placement_index     INT           NOT NULL,
    raw_blob            JSON          NULL,

    created_at          DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    created_by_version  VARCHAR(32)   NOT NULL,
    source_artifact_ids JSON          NOT NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uq_block_placement (map_id, parser_version, placement_index),
    KEY ix_block_placements_family_type (block_family, block_type),
    KEY ix_block_placements_map_parser (map_id, parser_version),

    CONSTRAINT fk_block_placements_map
        FOREIGN KEY (map_id) REFERENCES maps (id)
        ON DELETE CASCADE
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;
