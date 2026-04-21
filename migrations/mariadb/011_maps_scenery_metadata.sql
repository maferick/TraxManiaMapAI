-- Map-level scenery metadata. Kept deliberately lightweight:
-- aggregate counts + a mood string + a per-map decoration_parse_status,
-- NOT a per-item placement table. Full scenery persistence is out of
-- scope until we can justify the row volume against a concrete
-- downstream use case.
--
-- The four counts are not claimed to be disjoint:
--   scenery_item_count       = total anchored objects
--   signpost_count           = subset with a WaypointSpecialProperty
--   scenery_standard_item_count = Nadeo-authored items (usually includes signposts)
--   scenery_custom_item_count   = non-Nadeo authored items
--
-- decoration_parse_status is tracked separately from parse_status so
-- we can retro-fill scenery on already-parsed maps without having to
-- re-insert block_placements.

ALTER TABLE maps
    ADD COLUMN mood                         VARCHAR(32) NULL AFTER is_block_mode,
    ADD COLUMN decoration_id                VARCHAR(128) NULL AFTER mood,
    ADD COLUMN day_time_seconds             INT NULL AFTER decoration_id,
    ADD COLUMN dynamic_daylight             TINYINT(1) NULL AFTER day_time_seconds,
    ADD COLUMN scenery_item_count           INT NULL AFTER dynamic_daylight,
    ADD COLUMN signpost_count               INT NULL AFTER scenery_item_count,
    ADD COLUMN scenery_standard_item_count  INT NULL AFTER signpost_count,
    ADD COLUMN scenery_custom_item_count    INT NULL AFTER scenery_standard_item_count,
    ADD COLUMN has_custom_items             TINYINT(1) NULL AFTER scenery_custom_item_count,
    ADD COLUMN decoration_parse_status      ENUM('unparsed','success','partial','failed')
                                             NOT NULL DEFAULT 'unparsed'
                                             AFTER has_custom_items,
    ADD KEY ix_maps_mood (mood),
    ADD KEY ix_maps_decoration_parse_status (decoration_parse_status);
