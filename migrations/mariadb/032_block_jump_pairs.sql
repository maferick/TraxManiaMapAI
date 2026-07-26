-- Jumps, learned from the fact that every published map is finishable.
--
-- The corpus-finishable axiom does the work here: these maps were
-- published and parse cleanly, so they can be driven. Therefore any gap
-- the racing line MUST cross is drivable, and a jump is not "two blocks
-- that happen to sit near each other" — that was the failed definition,
-- and 81% of radius-3 grammar rows were exactly that coincidence.
--
-- A jump is an OPEN END facing another OPEN END across a gap:
--
--   * block A has a face with no route block adjacent to it, so the
--     racing line stops there
--   * along that same direction, 2..N cells away, block B has an open
--     face pointing back
--   * the map is finishable, so the car gets from A to B somehow, and
--     with nothing in between the only way is through the air
--
-- Populated by src/constraints/route_sequences.py::build_jumps.

CREATE TABLE IF NOT EXISTS block_jump_pairs (
    jump_signature     CHAR(64)     NOT NULL,
    block_a            VARCHAR(255) NOT NULL,
    block_b            VARCHAR(255) NOT NULL,
    -- Take-off to landing, in A's rotation frame.
    dx                 SMALLINT     NOT NULL,
    dy                 SMALLINT     NOT NULL,
    dz                 SMALLINT     NOT NULL,
    rel_rotation       TINYINT      NOT NULL,
    -- Chebyshev gap in XZ: how many empty cells the car flies over.
    gap                TINYINT      NOT NULL,
    environment        VARCHAR(64)  NOT NULL DEFAULT 'Stadium2020',
    occurrences        BIGINT       NOT NULL DEFAULT 0,
    map_count          BIGINT       NOT NULL DEFAULT 0,
    created_at         DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at         DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                    ON UPDATE CURRENT_TIMESTAMP(6),
    created_by_version VARCHAR(64)  NOT NULL,
    PRIMARY KEY (jump_signature),
    KEY idx_jump_from (block_a, environment, map_count),
    KEY idx_jump_gap (gap, map_count)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
