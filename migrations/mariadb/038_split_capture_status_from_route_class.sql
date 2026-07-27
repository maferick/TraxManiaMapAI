-- Separate capture validity from route classification.
--
-- 037 conflated the two into one `classification` column, and that
-- immediately produced a wrong answer: the three captures whose ghost
-- never entered the scene were labelled `unresolved_elevated` at 0%
-- coverage. Those samples all sit at the origin, so there is no
-- trajectory at all. That is a CAPTURE failure, and describing it as a
-- property of the map's route both blames the map for a rig problem and
-- drags the cohort distribution down with rows that measure nothing.
--
-- Two independent axes:
--
--   capture_status       did we obtain a usable trajectory?
--                        valid | no_movement | incomplete | corrupt
--                        | capture_failed
--
--   route_surface_class  given a usable trajectory, what is the car on?
--                        block_covered | terrain_offroad
--                        | unresolved_elevated | mixed
--
-- route_surface_class is NULL whenever capture_status <> 'valid',
-- because the question is not answerable. Every coverage denominator
-- must filter on capture_status = 'valid'; the invalid captures are
-- reported separately with their failure reason.
--
-- Both are VARCHAR rather than ENUM: these vocabularies are still being
-- discovered, and an ENUM would force a migration each time a new
-- failure mode shows up in the bulk run.

ALTER TABLE replay_telemetry_coverage
    ADD COLUMN IF NOT EXISTS capture_status VARCHAR(32) NOT NULL
        DEFAULT 'valid' AFTER item_ingestion_version,
    ADD COLUMN IF NOT EXISTS capture_failure_reason TEXT NULL
        AFTER capture_status,
    ADD COLUMN IF NOT EXISTS route_surface_class VARCHAR(32) NULL
        AFTER capture_failure_reason,
    ADD COLUMN IF NOT EXISTS route_surface_confidence DOUBLE NULL
        AFTER route_surface_class,
    ADD COLUMN IF NOT EXISTS route_surface_reason TEXT NULL
        AFTER route_surface_confidence;

ALTER TABLE replay_telemetry_coverage
    ADD KEY IF NOT EXISTS ix_rtc_capture_status (capture_status),
    ADD KEY IF NOT EXISTS ix_rtc_route_class (route_surface_class);

-- The old single-axis columns. Dropped rather than left behind: the
-- table is new and every consumer of it is in this repo, so keeping a
-- column that mixes the two axes only invites someone to read it.
ALTER TABLE replay_telemetry_coverage
    DROP COLUMN IF EXISTS classification,
    DROP COLUMN IF EXISTS classification_confidence,
    DROP COLUMN IF EXISTS classification_reason;
