-- ORDERED block sequences along a reconstructed racing line.
--
-- Everything else in this schema is co-occurrence, which is
-- direction-blind (a symmetric straight records the same scene under
-- rotation 0 and 2, so "what follows what" splits evenly) and cannot
-- express a sequence. This table has both, because the row is taken
-- from a chain walked Start -> Finish rather than from a neighbour
-- scan: `block_a` really does come before `block_b`.
--
--   n = 2   a directional pair
--   n = 3   a triple, so a chicane-into-straight-into-booster pattern
--           is representable at all
--
-- Populated by src/constraints/route_sequences.py.

CREATE TABLE IF NOT EXISTS block_route_sequences (
    seq_signature      CHAR(64)     NOT NULL,
    n                  TINYINT      NOT NULL,
    block_a            VARCHAR(255) NOT NULL,
    block_b            VARCHAR(255) NOT NULL,
    block_c            VARCHAR(255) NOT NULL DEFAULT '',
    -- A -> B, in A's rotation frame.
    dx1                SMALLINT     NOT NULL,
    dy1                SMALLINT     NOT NULL,
    dz1                SMALLINT     NOT NULL,
    rel1               TINYINT      NOT NULL,
    -- B -> C, in B's rotation frame. Zero when n = 2.
    dx2                SMALLINT     NOT NULL DEFAULT 0,
    dy2                SMALLINT     NOT NULL DEFAULT 0,
    dz2                SMALLINT     NOT NULL DEFAULT 0,
    rel2               TINYINT      NOT NULL DEFAULT 0,
    environment        VARCHAR(64)  NOT NULL DEFAULT 'Stadium2020',
    occurrences        BIGINT       NOT NULL DEFAULT 0,
    -- Breadth, which is what generation should weight on: one map with
    -- a forty-block straight run must not outvote forty maps.
    map_count          BIGINT       NOT NULL DEFAULT 0,
    created_at         DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at         DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                    ON UPDATE CURRENT_TIMESTAMP(6),
    created_by_version VARCHAR(64)  NOT NULL,
    PRIMARY KEY (seq_signature),
    KEY idx_seq_pair (n, block_a, environment, map_count),
    KEY idx_seq_triple (n, block_a, block_b, environment, map_count)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
