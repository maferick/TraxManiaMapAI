-- Observed placement grammar: what mappers actually build.
--
-- Supersedes clip-derived rules as the definition of a valid
-- transition. `block_face_transitions` only counted pairs whose
-- route-clips matched on touching faces, which encodes ONE way to
-- build a track and silently excludes things real maps do constantly:
--
--   * jumps      -- takeoff and landing separated by a gap, no clips
--   * gates      -- GateCheckpoint has no clips at all and is placed
--                   OVER the route rather than chained into it
--   * platforms  -- close with PlatformTechStart + GateExpandableFinish
--   * cross-family joins that no shared clip explains
--
-- A row here means: in `map_count` distinct maps, block B was observed
-- at this offset and relative rotation from block A. Validity is
-- evidence, not derivation.
--
-- The natural key is wide (2 varchars + 4 ints + env), so the PK is a
-- sha256 signature, matching block_triple_transitions.

CREATE TABLE IF NOT EXISTS block_placement_grammar (
    pair_signature     CHAR(64)     NOT NULL,
    block_a            VARCHAR(255) NOT NULL,
    block_b            VARCHAR(255) NOT NULL,
    -- Offset of B's anchor from A's, in A's own rotation frame, so a
    -- pattern learned facing north applies at every heading.
    dx                 SMALLINT     NOT NULL,
    dy                 SMALLINT     NOT NULL,
    dz                 SMALLINT     NOT NULL,
    rel_rotation       TINYINT      NOT NULL,
    environment        VARCHAR(64)  NOT NULL DEFAULT '',
    -- Whether a route-clip actually matched across the touching faces.
    -- Kept as a SIGNAL (clip-matched pairs are safer) rather than a
    -- filter, so jumps and clipless gates survive.
    clip_matched       TINYINT(1)   NOT NULL DEFAULT 0,
    pair_count         BIGINT       NOT NULL DEFAULT 0,
    map_count          BIGINT       NOT NULL DEFAULT 0,
    created_at         DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at         DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                    ON UPDATE CURRENT_TIMESTAMP(6),
    created_by_version VARCHAR(64)  NOT NULL,
    PRIMARY KEY (pair_signature),
    KEY idx_grammar_from (block_a, environment, map_count),
    KEY idx_grammar_to (block_b, environment)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
