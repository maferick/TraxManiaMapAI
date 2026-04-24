-- Phase 2 #218-2 — block-transition pattern model, triple layer.
--
-- Counts ordered (A → B → C) triple transitions. Same extraction
-- as pairs (#218-1): walk route_corridors.path_cells, look up the
-- block at each cell, emit triple counts per environment.
--
-- Schema note: the composite natural key (6 varchars + environment)
-- overflows MariaDB's 3072-byte index limit even with tight sizes.
-- We use a client-computed sha256 signature as the PK; the seven
-- original columns remain queryable at full width (192 char names).
-- Python-side signature computation means we don't depend on
-- MariaDB's GENERATED column + ON DUPLICATE KEY UPDATE interaction.

CREATE TABLE IF NOT EXISTS block_triple_transitions (
    transition_signature CHAR(64)     NOT NULL PRIMARY KEY,

    block_family_a    VARCHAR(64)  NOT NULL,
    block_name_a      VARCHAR(192) NOT NULL,
    block_family_b    VARCHAR(64)  NOT NULL,
    block_name_b      VARCHAR(192) NOT NULL,
    block_family_c    VARCHAR(64)  NOT NULL,
    block_name_c      VARCHAR(192) NOT NULL,
    environment       VARCHAR(48)  NOT NULL DEFAULT '',

    transition_count  BIGINT       NOT NULL DEFAULT 0,
    map_count         BIGINT       NOT NULL DEFAULT 0,

    created_at        DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at        DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                   ON UPDATE CURRENT_TIMESTAMP(6),
    created_by_version VARCHAR(32) NOT NULL,

    KEY idx_triple_count (transition_count DESC),
    -- Secondary on family-level only for roll-up queries.
    KEY idx_family_triple (block_family_a, block_family_b, block_family_c)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
