-- Phase 2 #218-6 — block_geometry extensions for classifier v1.1.
--
-- Adds two soft signals next to the existing v1.0 columns. Both stay
-- non-load-bearing (project-218 rule: geometry is a soft signal, never
-- overrides traversability evidence). Rebuild the table with the new
-- classifier version after applying this migration.
--
--   placement_mode   — did the corpus place this (family, name) only
--                      on the grid, only as a free block, or mixed?
--                      Derived from aggregation over block_placements.
--                      Generators that restrict themselves to grid
--                      placements can filter on placement_mode IN
--                      ('grid_only','mixed') and ignore 'free_only'
--                      blocks whose footprint is meaningless without
--                      the free-block yaw/pitch/roll.
--
--   connector_hint   — textual label for how a block's drivable exits
--                      are oriented at rotation 0. Values mirror the
--                      constants in src.constraints.block_geometry:
--                        straight_x, curve_xz, slope_xy, loop_y,
--                        platform, anchor, ''.
--                      Used by the geometry validator (PR after this
--                      one) and by generation scoring.

ALTER TABLE block_geometry
    ADD COLUMN placement_mode ENUM(
        'grid_only',
        'free_only',
        'mixed',
        'unknown'
    ) NOT NULL DEFAULT 'unknown' AFTER footprint_z,
    ADD COLUMN connector_hint VARCHAR(32) NOT NULL DEFAULT '' AFTER placement_mode,
    ADD KEY idx_placement_mode (placement_mode),
    ADD KEY idx_connector_hint (connector_hint);
