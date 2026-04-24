-- Phase 2 PR M — finishability-proof metadata for source maps.
--
-- Separate table (not columns on `maps`) so the proof layer stays
-- pluggable: future signals (replay counts, leaderboard snapshots,
-- telemetry-derived finish evidence) can extend without churning
-- the hot `maps` row.
--
-- Hard boundary from scope-v0.1 §Level-2 addendum: this metadata is
-- SOURCE-SIDE evidence only. The generator's finishability gate
-- still runs mandatorily on every generated map. Proof here never
-- bypasses the gate.
--
-- WR time: computed at write time from
--   MIN(finish_time_ms) FROM replays WHERE map_id = ?
--                                      AND clean_status IN ('clean', 'usable_with_warnings').
-- No TMX leaderboard scraping — we already have replays, so the
-- fastest finish_time_ms is our operational WR.

CREATE TABLE IF NOT EXISTS map_finishability_proof (
    map_id                  BIGINT       NOT NULL PRIMARY KEY,

    -- Author-set medal times from the GBX (milliseconds). NULL when
    -- the map author didn't set that medal.
    author_time_ms          BIGINT       NULL,
    bronze_time_ms          BIGINT       NULL,
    silver_time_ms          BIGINT       NULL,
    gold_time_ms            BIGINT       NULL,

    -- Fastest clean finish across our replays table, if any. Null
    -- when the map has no clean replays.
    world_record_time_ms    BIGINT       NULL,
    world_record_replay_id  BIGINT       NULL,

    -- Whether the author set an author time on the map itself.
    has_author_time         TINYINT(1)   NOT NULL DEFAULT 0,
    -- Whether we've seen at least one clean replay with a finish
    -- time. Equivalent to world_record_time_ms IS NOT NULL, stored
    -- separately so readers can distinguish "author set no time
    -- but players have finished" from "no evidence at all."
    has_world_record        TINYINT(1)   NOT NULL DEFAULT 0,

    -- Derived precedence; the renderer picks this to decide which
    -- badge to show. Order from strongest to weakest evidence:
    --   replay          — a clean replay with finish_time_ms exists
    --   author_time     — author set an author time on the GBX
    --   world_record    — some replay exists but none marked clean
    --   internal_route  — only our corridor gate says so
    --   none            — no evidence yet
    proof_source ENUM(
        'replay',
        'author_time',
        'world_record',
        'internal_route',
        'none'
    )                                    NOT NULL DEFAULT 'none',

    recorded_at             DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    created_by_version      VARCHAR(32)  NOT NULL,

    CONSTRAINT map_finishability_proof_map_fk
        FOREIGN KEY (map_id) REFERENCES maps(id) ON DELETE CASCADE,
    KEY idx_proof_source (proof_source),
    KEY idx_has_flags    (has_author_time, has_world_record)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
