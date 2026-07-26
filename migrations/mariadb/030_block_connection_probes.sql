-- What the GAME said about a block pair, as opposed to what the
-- corpus contains or what clip data implies.
--
-- Three different questions live in three different columns here, and
-- conflating them is what produced every broken generated map:
--
--   editor_ok     the game PERMITS this geometry. Occupancy only —
--                 measured: the editor places a block floating in
--                 mid-air, and MobilVariantIndex is 0 whether a block
--                 is joined, unjoined or isolated. Acceptance is NOT
--                 connection.
--   clip_matched  the two surfaces MEET. Carried over from
--                 block_placement_grammar; this is what predicts the
--                 dead-end barrier the game draws on an unjoined end.
--   map_count     real mappers DO it, over 18,935 Stadium2020 maps.
--
-- Populated by tools/probe_connections.py, which places each pair
-- alone in a blank map through the editor's own placement path.

CREATE TABLE IF NOT EXISTS block_connection_probes (
    probe_signature     CHAR(64)     NOT NULL,
    block_a             VARCHAR(255) NOT NULL,
    block_b             VARCHAR(255) NOT NULL,
    -- Offset of B from A, in A's own rotation frame, matching
    -- block_placement_grammar so the two tables join.
    dx                  SMALLINT     NOT NULL,
    dy                  SMALLINT     NOT NULL,
    dz                  SMALLINT     NOT NULL,
    rel_rotation        TINYINT      NOT NULL,
    environment         VARCHAR(64)  NOT NULL DEFAULT 'Stadium2020',
    map_count           BIGINT       NOT NULL DEFAULT 0,
    clip_matched        TINYINT(1)   NOT NULL DEFAULT 0,
    -- Per-block, because a refusal can belong to either one.
    editor_a_placed     TINYINT(1)   NOT NULL,
    editor_b_placed     TINYINT(1)   NOT NULL,
    -- What our own footprint + rotation model predicted. A row where
    -- this disagrees with the editor is a bug with an address; that
    -- model has been wrong twice before.
    offline_predicts_ok TINYINT(1)   NOT NULL,
    -- Probe altitude. Below row 9 the game refuses everything, which
    -- looks like a pair rule and is really just the ground.
    base_y              SMALLINT     NOT NULL,
    created_at          DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    created_by_version  VARCHAR(64)  NOT NULL,
    PRIMARY KEY (probe_signature),
    KEY idx_probe_from (block_a, environment),
    KEY idx_probe_verdict (editor_a_placed, editor_b_placed, clip_matched)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
