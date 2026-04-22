-- Additional TMX-derived quality signals, pulled from the v2
-- /api/maps summary endpoint. Both are nullable because they're
-- enrichments over the baseline summary — existing rows predating
-- this migration stay NULL until a re-ingest populates them.
--
--   track_value — TMX's computed "value of a map towards the MX
--                 Leaderboard" (Int32). Less popularity-biased than
--                 award_count; TMX's own leaderboard ranking uses it.
--   difficulty  — author-declared difficulty level (Int 0-5 in v2 —
--                 the string form is v1-only and annotated
--                 TM2/SM-only anyway). Kept as integer; the
--                 Beginner/Intermediate/... mapping is a display
--                 concern and lives on the consumer side.

ALTER TABLE maps
    ADD COLUMN track_value INT NULL AFTER popularity_metric,
    ADD COLUMN difficulty  INT NULL AFTER track_value,
    ADD KEY ix_maps_track_value (track_value),
    ADD KEY ix_maps_difficulty (difficulty);
