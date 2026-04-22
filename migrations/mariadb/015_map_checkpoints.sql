-- Checkpoint / start / finish trigger positions extracted from
-- CGameCtnBlock.WaypointSpecialProperty during map parse. Each row is
-- one waypoint-bearing block; a single logical waypoint may span
-- multiple rows when the underlying block is multi-cell (e.g.
-- GateExpandableFinish occupies several adjacent grid cells, each
-- reporting the same Tag + Order).
--
-- `tag` is a free-form string, NOT an enum — TM2020 ships at least
-- {"Spawn","Goal","Checkpoint","LinkedCheckpoint","StartFinish"} and
-- Nadeo adds new waypoint types with client updates. A column enum
-- would force a migration for every new tag; a VARCHAR lets the
-- wrapper emit what it sees and downstream code decide how to handle
-- unknown values.
--
-- `waypoint_order` is non-zero only for `LinkedCheckpoint` tags
-- (multilap / ordered-sequence maps). For plain `Checkpoint` tags it
-- is always 0 — the race order is resolved per-replay by the game at
-- runtime, not pre-baked into the map. Corridor inference resolves
-- ordering against replay checkpoint_times_ms; this table doesn't
-- pretend to know it.
--
-- Position is stored in either (x, y, z) for grid-placed blocks or
-- (abs_x, abs_y, abs_z) for free-placed blocks, matching the discriminator
-- already in block_placements. Both sets nullable; `placement` tells
-- consumers which to read.

CREATE TABLE map_checkpoints (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    map_id BIGINT NOT NULL,
    parser_version VARCHAR(32) NOT NULL,
    waypoint_index INT NOT NULL,
    tag VARCHAR(32) NOT NULL,
    waypoint_order INT NOT NULL DEFAULT 0,
    block_name VARCHAR(255) NOT NULL,
    placement ENUM('grid', 'free') NOT NULL,
    x INT NULL,
    y INT NULL,
    z INT NULL,
    abs_x DOUBLE NULL,
    abs_y DOUBLE NULL,
    abs_z DOUBLE NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_map_checkpoints_map
        FOREIGN KEY (map_id) REFERENCES maps (id) ON DELETE CASCADE,
    KEY ix_map_checkpoints_map (map_id, parser_version),
    KEY ix_map_checkpoints_tag (tag),
    -- One logical waypoint row per (map, parser_version, waypoint_index).
    -- Re-parsing a map under a new parser_version keeps the old rows
    -- available until an explicit delete — matches the re-ingestion
    -- versioning policy in CLAUDE.md.
    UNIQUE KEY uq_map_checkpoints_slot (map_id, parser_version, waypoint_index)
);
