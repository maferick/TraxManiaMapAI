-- Baked block placements.
--
-- A THIRD placement collection, separate from Blocks and
-- AnchoredObjects, and a demonstrated matcher dependency rather than a
-- speculative one.
--
-- WHY: the worst unmatched gap inside the block_covered cohort
-- (87 m / 3.3 s on `randomstuff5`, a map at 85.3% coverage) was the car
-- driving dead flat at y=130.0 for 50 samples, a_y ~ 0, 81->106 km/h,
-- correctly not airborne. block_placements holds NOTHING at that row
-- for that map. BakedBlocks holds `OpenTechRoadFC` there, and the map
-- carries 61 of them. TM2020 bakes the rendered surface of "open" road
-- into BakedBlocks while only the editor block reaches Blocks. So this
-- is drivable geometry that no existing table records, and its absence
-- is invisible in aggregate coverage.
--
-- Deliberately NOT merged into map_items even though one dump command
-- emits both. They are different collections with different semantics:
-- an item is author-placed with a transform, a baked block is
-- game-generated on the grid.
--
-- OBSERVATION LAYER ONLY. `OpenTechRoadFC` and friends are NOT in the
-- placeable block catalogue (checked: only `Grass` is), so they can
-- never become a construction token. A generator must never emit
-- `PLACE OpenTechRoadFC`. For training export these rows either resolve
-- to the originating placeable editor block, appear as
-- TRAVERSE_DERIVED_SURFACE, or are excluded from supervised
-- construction targets.
--
-- SIZE: 5,615,074 baked blocks across the 545-map gold set (max 134,364
-- in one map), of which 1,255,680 are the `Grass` base-terrain layer
-- (2,304 per map, the full 48x48 ground). Base terrain is flagged, not
-- special-cased away, but it is redundant for matching because
-- TERRAIN_GROUND already handles ground-plane driving. Populate the
-- captured cohort first.
--
-- Field surface verified by reflection against the installed GBX.NET,
-- not taken from the file-format documentation: BakedBlocks are full
-- CGameCtnBlock objects carrying Coord, Direction, Flags, Variant,
-- SubVariant, IsGround, IsClip, IsGhost, IsPillar, a BlockModel Ident
-- and a WaypointSpecialProperty.
--
-- NOT BUILT, deliberately: `BakedClipsAdditionalData` is empty on
-- 0 of 545 maps, so a clip-topology table would have been an empty
-- table. Re-check if a future title update starts populating it.

CREATE TABLE IF NOT EXISTS map_baked_blocks (
    id                    BIGINT        NOT NULL AUTO_INCREMENT,
    map_id                BIGINT        NOT NULL,
    parser_version        VARCHAR(32)   NOT NULL,

    block_name            VARCHAR(255)  NOT NULL,
    model_id              VARCHAR(255)  NULL,
    model_collection      VARCHAR(64)   NULL,
    model_author          VARCHAR(128)  NULL,

    x                     INT           NULL,
    y                     INT           NULL,
    z                     INT           NULL,
    direction             SMALLINT      NULL,

    abs_x                 DOUBLE        NULL,
    abs_y                 DOUBLE        NULL,
    abs_z                 DOUBLE        NULL,

    flags                 INT           NULL,
    variant               SMALLINT      NULL,
    sub_variant           SMALLINT      NULL,

    is_ground             TINYINT(1)    NOT NULL DEFAULT 0,
    is_clip               TINYINT(1)    NOT NULL DEFAULT 0,
    is_free               TINYINT(1)    NOT NULL DEFAULT 0,
    is_ghost              TINYINT(1)    NOT NULL DEFAULT 0,
    is_pillar             TINYINT(1)    NOT NULL DEFAULT 0,

    waypoint_tag          VARCHAR(32)   NULL,
    waypoint_order        INT           NULL,
    waypoint_raw          JSON          NULL,

    -- Is this the auto-generated ground layer rather than built
    -- geometry? Every map carries exactly 2,304 `Grass` cells.
    base_terrain          TINYINT(1)    NOT NULL DEFAULT 0,

    -- Surface role stays `unknown` until replay evidence or metadata
    -- supports road / zone / decoration. Guessing from the name would
    -- bake a naming convention into the data model, and these names are
    -- absent from the catalogue precisely because they are internal.
    surface_role          VARCHAR(16)   NOT NULL DEFAULT 'unknown',

    -- Where a footprint came from, and how much to trust it. These
    -- blocks are not in the catalogue, so `anchor_only` is the honest
    -- default rather than an invented extent.
    footprint_source      VARCHAR(16)   NOT NULL DEFAULT 'anchor_only',
    footprint_confidence  DOUBLE        NULL,

    -- Eventual association back to the placeable editor block that
    -- generated this surface. Unresolved for now: BakedClipsAdditionalData
    -- was the candidate evidence and it is empty corpus-wide.
    parent_block_id       BIGINT        NULL,

    placement_index       INT           NOT NULL,

    created_at            DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    created_by_version    VARCHAR(32)   NOT NULL,
    source_artifact_ids   JSON          NOT NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uq_baked_placement (map_id, placement_index),
    KEY ix_baked_cell (map_id, x, y, z),
    KEY ix_baked_name (block_name),
    KEY ix_baked_terrain (map_id, base_terrain),
    KEY ix_baked_waypoint (waypoint_tag),

    CONSTRAINT fk_baked_map
        FOREIGN KEY (map_id) REFERENCES maps (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
