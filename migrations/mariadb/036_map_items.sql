-- Item (anchored-object) placements.
--
-- block_placements covers CGameCtnBlock only. Items are
-- CGameCtnAnchoredObject and were never ingested anywhere, so an
-- item-built track had no rows to match telemetry against.
--
-- WHY THIS IS WORTH THE ROWS: on the pilot, adding item anchors as
-- candidates moved checkpoint-region coverage from 0.0% to 65.6% on one
-- map and from 70% to 100% on another, because on item-built maps the
-- WAYPOINTS ARE ITEMS. Surface coverage barely moved (+3.5 to +6.8pp).
-- So this table is justified by route pinning, not by surface matching.
--
-- ROI OF ITEM GEOMETRY IS UNKNOWN, NOT DISPROVEN. The small surface
-- gain above shows only that an anchor cell is a poor proxy for a large
-- item's extent; a trajectory may well be riding item surfaces whose
-- anchors sit several cells away. Mesh parsing / an item catalogue is
-- deferred pending the prevalence of elevated unresolved trajectories
-- across the captured cohort, not written off.
--
-- SIZE. 15,262,391 items across the 18,935-map Stadium2020 corpus
-- (mean 806, max 97,599); 646,125 for the 545-map linked-checkpoint
-- gold set. For comparison block_placements holds 82,985,631 rows at
-- 18.7 GB data + 8.4 GB index. This table is populated for the
-- captured/gold cohort first; a full-corpus backfill is a separate
-- decision with its own disk check.
--
-- Coordinates. abs_* is the item's own AbsolutePositionInMap in metres.
-- block_unit_* is the source BlockUnitCoord exactly as the file reports
-- it. cell_* is DERIVED from abs_* through the calibrated grid
-- (x/32, 9+(y-8)/8, z/32). Source and derived are kept apart on
-- purpose: they agreed exactly on the item checked by hand
-- (abs <767,18,858> -> (23,10,26) = reported BlockUnitCoord), and any
-- future disagreement is a calibration signal that must not be masked
-- by overwriting one with the other.
--
-- Precision. DOUBLE throughout for position, rotation, scale and pivot.
-- The source values are floats and rounding them at ingest would lose
-- precision that cannot be recovered without reparsing.

CREATE TABLE IF NOT EXISTS map_items (
    id                    BIGINT        NOT NULL AUTO_INCREMENT,
    map_id                BIGINT        NOT NULL,
    parser_version        VARCHAR(32)   NOT NULL,

    -- Full ItemModel identity, kept as three fields. The same item id
    -- exists under different collections and authors with different
    -- geometry, so collapsing these into one string would make the key
    -- ambiguous exactly where custom items matter.
    item_id               VARCHAR(255)  NOT NULL,
    item_collection       VARCHAR(64)   NOT NULL,
    item_author           VARCHAR(128)  NOT NULL,

    abs_x                 DOUBLE        NULL,
    abs_y                 DOUBLE        NULL,
    abs_z                 DOUBLE        NULL,

    block_unit_x          INT           NULL,
    block_unit_y          INT           NULL,
    block_unit_z          INT           NULL,

    cell_x                INT           NULL,
    cell_y                INT           NULL,
    cell_z                INT           NULL,

    pitch                 DOUBLE        NULL,
    yaw                   DOUBLE        NULL,
    roll                  DOUBLE        NULL,
    scale                 DOUBLE        NULL,

    pivot_x               DOUBLE        NULL,
    pivot_y               DOUBLE        NULL,
    pivot_z               DOUBLE        NULL,

    flags                 INT           NULL,

    -- Waypoint role. tag is free-form, NOT an enum: TM2020 ships at
    -- least Spawn / Goal / Checkpoint / LinkedCheckpoint / StartFinish
    -- and Nadeo adds types with client updates, so an enum would force
    -- a migration per game patch. waypoint_order is non-zero only on
    -- LinkedCheckpoint. waypoint_raw keeps the full reflected object so
    -- a later pass can extract fields we cannot name today without
    -- reparsing every map.
    waypoint_tag          VARCHAR(32)   NULL,
    waypoint_order        INT           NULL,
    waypoint_raw          JSON          NULL,

    placement_index       INT           NOT NULL,

    created_at            DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    created_by_version    VARCHAR(32)   NOT NULL,
    source_artifact_ids   JSON          NOT NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uq_map_items_placement (map_id, placement_index),
    KEY ix_map_items_cell (map_id, cell_x, cell_y, cell_z),
    KEY ix_map_items_identity (item_id, item_collection, item_author),
    KEY ix_map_items_waypoint (waypoint_tag),

    CONSTRAINT fk_map_items_map
        FOREIGN KEY (map_id) REFERENCES maps (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
