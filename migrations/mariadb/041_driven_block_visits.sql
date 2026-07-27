-- Ordered driven-block sequences extracted from ghost telemetry.
--
-- This is the ground truth the generation model trains on: for each
-- valid capture, the ordered list of blocks the author's own
-- validation ghost actually rode, with explicit AIRBORNE and
-- OFF_SURFACE stretches where it rode nothing. Produced by the
-- driven-path Viterbi (src/route/driven_path.py); validated against
-- the ghost's independent checkpoint splits.
--
-- Non-block states are first-class rows, not gaps: a jump or an
-- off-surface stretch is part of the driven route, and a training
-- export that silently stitched across them would teach transitions
-- nobody drove. placement_id is NULL exactly for those rows.
--
-- extractor_version is part of the unique key so a better extractor
-- writes a new sequence next to the old one rather than erasing the
-- history a model may have trained on.

CREATE TABLE IF NOT EXISTS driven_block_visits (
    id                  BIGINT       NOT NULL AUTO_INCREMENT,
    replay_id           BIGINT       NOT NULL,
    extractor_version   VARCHAR(32)  NOT NULL,

    visit_index         INT          NOT NULL,
    -- 'block' | 'airborne' | 'off_surface'
    state               VARCHAR(16)  NOT NULL,
    placement_id        BIGINT       NULL,
    block_type          VARCHAR(255) NULL,

    first_sample        INT          NOT NULL,
    last_sample         INT          NOT NULL,
    enter_ms            BIGINT       NOT NULL,
    exit_ms             BIGINT       NOT NULL,

    created_at          DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    created_by_version  VARCHAR(32)  NOT NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uq_dbv (replay_id, extractor_version, visit_index),
    KEY ix_dbv_replay (replay_id),
    KEY ix_dbv_block (block_type),

    CONSTRAINT fk_dbv_replay
        FOREIGN KEY (replay_id) REFERENCES replays (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
