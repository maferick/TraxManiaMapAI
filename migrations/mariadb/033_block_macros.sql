-- Recurring multi-block runs: the units a planner composes with.
--
-- The ordered triples in block_route_sequences were measured as a
-- per-step weight and made maps WORSE at every setting, because the
-- strongest triples are same-block runs and rewarding them rewards
-- repetition. That was the wrong granularity, not the wrong data.
--
-- A macro is the right granularity. `SpecialTurbo2 x3` stops being a
-- weight nudge and becomes a booster section placed as a unit.
--
-- Direction-ambiguous by construction (extraction is opposing-face,
-- not a driving order), so a run and its reverse are stored once under
-- a canonical representative.

CREATE TABLE IF NOT EXISTS block_macros (
    macro_signature    CHAR(64)     NOT NULL,
    length             TINYINT      NOT NULL,
    -- Ordered block ids.
    blocks_json        TEXT         NOT NULL,
    -- One [dx, dy, dz, rel_rotation] per step, each in the PREVIOUS
    -- block's frame — the same arithmetic a single move uses, so a
    -- macro applies at any heading.
    steps_json         TEXT         NOT NULL,
    environment        VARCHAR(64)  NOT NULL DEFAULT 'Stadium2020',
    occurrences        BIGINT       NOT NULL DEFAULT 0,
    map_count          BIGINT       NOT NULL DEFAULT 0,
    created_at         DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at         DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                    ON UPDATE CURRENT_TIMESTAMP(6),
    created_by_version VARCHAR(64)  NOT NULL,
    PRIMARY KEY (macro_signature),
    KEY idx_macro_len (length, environment, map_count)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
