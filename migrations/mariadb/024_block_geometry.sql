-- Phase 2 #218-3 — block-geometry catalogue.
--
-- One row per distinct (block_family, block_name) seen in the corpus.
-- Stores inferred shape / surface / connector metadata for the
-- generation-time pattern + geometry compatibility score.
--
-- v1 classifier is name-pattern inferred (fast, 99% coverage, brittle
-- for exotic custom blocks). Mesh-level geometry from the GBX is a
-- future enhancement; this row stores the v1 classifier's output plus
-- a classifier_version so reruns cleanly replace older rows.
--
-- Scope boundary (same as #218): purely a soft signal. No gate or
-- traversability decision depends on these fields; generation
-- weighting and strip-policy hints only.

CREATE TABLE IF NOT EXISTS block_geometry (
    block_family       VARCHAR(64)  NOT NULL,
    block_name         VARCHAR(192) NOT NULL,

    -- Inferred from name. Values match src.constraints.block_geometry
    -- ShapeClass enum. Unknown maps to 'unknown' rather than NULL so
    -- the join behaviour is predictable.
    shape_class        ENUM(
        'straight',
        'curve',
        'ramp',
        'loop',
        'platform',
        'support',
        'deco',
        'start',
        'checkpoint',
        'finish',
        'gate',
        'unknown'
    ) NOT NULL DEFAULT 'unknown',

    -- Coarse surface / material hint. Derived from family +
    -- name-fragment heuristics ('Dirt', 'Ice', 'Grass', 'Plastic',
    -- etc.). Empty string when not inferrable.
    surface_hint       VARCHAR(32)  NOT NULL DEFAULT '',

    -- Whether the block can legitimately carry a race-role waypoint
    -- (Spawn / Checkpoint / Finish / LinkedCheckpoint). Used by
    -- the strip-policy follow-up (#217) to preserve anchor geometry
    -- regardless of whether the chosen route steps through it.
    is_anchor_capable  TINYINT(1)   NOT NULL DEFAULT 0,

    -- Whether the block is structural deco that doesn't belong in
    -- the drivable graph (grass, trees, pillars classified elsewhere
    -- as non-drivable). Separate from shape_class='deco' because
    -- some support-class blocks are legitimately drivable.
    is_deco            TINYINT(1)   NOT NULL DEFAULT 0,

    -- Grid footprint at the block's base rotation (rotation=0).
    -- v1 classifier defaults all to 1; mesh-level accuracy is future
    -- work. Stored here so downstream readers already have the field
    -- without another migration.
    footprint_x        SMALLINT UNSIGNED NOT NULL DEFAULT 1,
    footprint_y        SMALLINT UNSIGNED NOT NULL DEFAULT 1,
    footprint_z        SMALLINT UNSIGNED NOT NULL DEFAULT 1,

    classifier_version VARCHAR(32)  NOT NULL,
    created_at         DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at         DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                    ON UPDATE CURRENT_TIMESTAMP(6),

    PRIMARY KEY (block_family, block_name),

    KEY idx_shape (shape_class),
    KEY idx_anchor_capable (is_anchor_capable),
    KEY idx_is_deco (is_deco)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
