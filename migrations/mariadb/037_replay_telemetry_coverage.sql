-- Per-capture telemetry coverage metrics.
--
-- Keyed per REPLAY, not per map. One map can carry an author validation
-- ghost and any number of leaderboard replays, and those follow
-- different racing lines: a route the author never drove can be well
-- covered while the author's own line is not, or the reverse. Keying
-- this per map would average two different questions into one number.
--
-- CONTINUOUS METRICS ONLY. No eligibility verdict is stored. Thresholds
-- for a usable/quarantined gate must be derived from the distribution
-- across the captured cohort, and the 17-map pilot is far too thin to
-- freeze one: it showed 14 maps at 82%+ and 3 below 39%, which is
-- suggestive but not a population. classification below records WHY a
-- capture looks poor, with a confidence, so the gate can be recomputed
-- later without re-running any matching.
--
-- The key includes everything that can change a number, so a re-run
-- under a new matcher or after item ingestion lands writes a NEW row
-- rather than overwriting history. That is what makes the effect of a
-- matcher change measurable instead of invisible.
--
--   matcher_version          block_matcher semantics
--   matcher_parameters_hash  row tolerance, free anchor/pad, etc.
--   telemetry_hash           the capture itself
--   item_ingestion_version   '' until items are populated for the map
--
-- TERRAIN_GROUND is a first-class observation, not missing data. Two of
-- three low-coverage pilot maps had 99-100% of their unmatched grounded
-- samples at grid row 9, absolute y = 8.0 exactly: the car is driving
-- on the grass terrain layer. Every map carries exactly 2,304 Grass
-- baked blocks covering the full 48x48 ground, so counting grass as
-- block coverage would push every ground-level map to ~100% while
-- meaning nothing. Terrain is therefore measured separately and counted
-- as neither covered nor missing. A TRAVERSE_TERRAIN representation for
-- the block-sequence model is still to be designed; until then
-- terrain-heavy routes stay their own cohort.

CREATE TABLE IF NOT EXISTS replay_telemetry_coverage (
    id                          BIGINT       NOT NULL AUTO_INCREMENT,

    -- The telemetry capture. Each capture is one replays row, so this
    -- is the capture id.
    replay_id                   BIGINT       NOT NULL,

    matcher_version             VARCHAR(32)  NOT NULL,
    matcher_parameters_hash     CHAR(64)     NOT NULL,
    telemetry_hash              CHAR(64)     NOT NULL,
    -- '' rather than NULL: MySQL treats NULLs as distinct in a unique
    -- key, which would let duplicate rows accumulate silently.
    item_ingestion_version      VARCHAR(32)  NOT NULL DEFAULT '',

    -- Airborne detection provenance. Currently the only available
    -- signal is ballistic vertical acceleration at the MEASURED TM2020
    -- free-fall of -24 m/s2 (well away from Earth gravity). A ghost
    -- exposes no wheel-contact surface through any typed API, so there
    -- is no direct game-state field to prefer or cross-check against.
    airborne_method             VARCHAR(32)  NOT NULL,
    airborne_method_version     VARCHAR(32)  NOT NULL,
    airborne_confidence         DOUBLE       NULL,

    samples_total               INT          NOT NULL,
    samples_airborne            INT          NOT NULL,
    samples_grounded            INT          NOT NULL,

    -- Candidate coverage split by source, so the contribution of each
    -- matching path stays visible instead of collapsing into one number.
    covered_grid                INT          NOT NULL DEFAULT 0,
    covered_free                INT          NOT NULL DEFAULT 0,
    covered_item                INT          NOT NULL DEFAULT 0,
    covered_any                 INT          NOT NULL DEFAULT 0,

    checkpoint_samples          INT          NOT NULL DEFAULT 0,
    checkpoint_covered          INT          NOT NULL DEFAULT 0,

    unmatched_samples           INT          NOT NULL DEFAULT 0,
    unmatched_distance_m        DOUBLE       NOT NULL DEFAULT 0,
    unmatched_duration_ms       BIGINT       NOT NULL DEFAULT 0,

    longest_gap_samples         INT          NOT NULL DEFAULT 0,
    longest_gap_m               DOUBLE       NOT NULL DEFAULT 0,
    longest_gap_ms              BIGINT       NOT NULL DEFAULT 0,

    terrain_ground_samples      INT          NOT NULL DEFAULT 0,
    terrain_ground_distance_m   DOUBLE       NOT NULL DEFAULT 0,
    terrain_ground_duration_ms  BIGINT       NOT NULL DEFAULT 0,

    -- Free-form, not an enum: the reasons are still being discovered.
    -- Seen so far: terrain_offroad, item_elevated, block_covered.
    classification              VARCHAR(32)  NULL,
    classification_confidence   DOUBLE       NULL,
    classification_reason       TEXT         NULL,

    created_at                  DATETIME(6)  NOT NULL
                                    DEFAULT CURRENT_TIMESTAMP(6),
    created_by_version          VARCHAR(32)  NOT NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uq_rtc_run (
        replay_id, matcher_version, matcher_parameters_hash,
        telemetry_hash, item_ingestion_version
    ),
    KEY ix_rtc_classification (classification),

    CONSTRAINT fk_rtc_replay
        FOREIGN KEY (replay_id) REFERENCES replays (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- Cells a capture crossed on terrain rather than on a route block.
--
-- Kept because TERRAIN_GROUND is an observation to model, not a hole to
-- paper over: a TRAVERSE_TERRAIN action needs to know WHERE the car
-- left the built track and which way it was pointing, not merely how
-- long it was off it. Aggregated per (capture, cell) so a map-sized
-- traversal stays bounded rather than one row per 50ms sample.

CREATE TABLE IF NOT EXISTS replay_terrain_cells (
    id                    BIGINT      NOT NULL AUTO_INCREMENT,
    replay_id             BIGINT      NOT NULL,
    matcher_version       VARCHAR(32) NOT NULL,

    cell_x                INT         NOT NULL,
    cell_y                INT         NOT NULL,
    cell_z                INT         NOT NULL,

    first_sample_index    INT         NOT NULL,
    sample_count          INT         NOT NULL,
    duration_ms           BIGINT      NOT NULL DEFAULT 0,
    distance_m            DOUBLE      NOT NULL DEFAULT 0,
    -- Mean heading in radians, atan2(dz, dx) averaged as a unit vector
    -- so that headings either side of the wrap point do not cancel.
    mean_heading_rad      DOUBLE      NULL,

    created_at            DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    created_by_version    VARCHAR(32) NOT NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uq_rtcells (replay_id, matcher_version, cell_x, cell_y, cell_z),
    KEY ix_rtcells_cell (cell_x, cell_y, cell_z),

    CONSTRAINT fk_rtcells_replay
        FOREIGN KEY (replay_id) REFERENCES replays (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
