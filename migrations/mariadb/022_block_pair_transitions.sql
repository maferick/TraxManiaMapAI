-- Phase 2 #218-1 — block-transition pattern model, pair layer.
--
-- Counts ordered (A → B) block transitions extracted from driven-
-- through paths (route_corridors.path_cells). Direction matters —
-- "Ramp then Curve" is different from "Curve then Ramp" — so the
-- (a, b) key is ordered, not normalized.
--
-- Source: for each route_corridors row, iterate consecutive cells
-- in path_cells. Look up the block at each cell in block_placements.
-- Emit one (fam_a, name_a) → (fam_b, name_b) transition per step.
-- Count all corridors equally for v1 (weighting by confidence /
-- chosen-vs-alternate ships with #218-4 scoring).
--
-- Why driven-through path cells and not raw block_placements:
-- - Path cells are already "evidence this pair is reachable" via the
--   corpus's route enumeration. Raw adjacency counts would include
--   blocks-next-to-blocks that no route uses (scenic deco stacks),
--   drowning the signal.
-- - Every map in the pair-counts table has gone through the gate +
--   assembly stack already; schema drift on map_checkpoints /
--   block_placements shapes doesn't invalidate these counts.

CREATE TABLE IF NOT EXISTS block_pair_transitions (
    -- Lengths sized for the observed corpus (family ≤ 17, block_type
    -- ≤ 172, environment a short string) with headroom. Keeping the
    -- composite PK under MariaDB's 3072-byte key limit matters — the
    -- PK is five VARCHARs and utf8mb4 is 4 bytes/char.
    block_family_a    VARCHAR(64)  NOT NULL,
    block_name_a      VARCHAR(192) NOT NULL,
    block_family_b    VARCHAR(64)  NOT NULL,
    block_name_b      VARCHAR(192) NOT NULL,
    environment       VARCHAR(48)  NOT NULL DEFAULT '',
    transition_count  BIGINT       NOT NULL DEFAULT 0,

    -- Per-map counts, for disambiguation of outlier single-map spikes.
    -- One distinct map contributing 1000 transitions of rare pair X is
    -- a very different signal from 1000 maps contributing 1 each.
    map_count         BIGINT       NOT NULL DEFAULT 0,

    created_at        DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at        DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                   ON UPDATE CURRENT_TIMESTAMP(6),
    created_by_version VARCHAR(32) NOT NULL,

    PRIMARY KEY (block_family_a, block_name_a,
                 block_family_b, block_name_b, environment),

    KEY idx_pair_count (transition_count DESC),
    KEY idx_family_pair (block_family_a, block_family_b)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
