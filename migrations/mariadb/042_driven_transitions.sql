-- Observed block-to-block transitions, mined from driven paths.
--
-- This is what block_route_sequences (031) tried to be and could not.
-- That table was mined from map GEOMETRY, where order is a guess, and
-- consuming it as a per-step weight made generated maps measurably
-- worse (see docs + memory: the strongest geometric triples ARE
-- same-block runs, so rewarding them rewards repetition). These rows
-- come from driven_block_visits: the order is not inferred, it was
-- WATCHED, one validation ghost per map, teleport-free sequences,
-- checkpoint-validated.
--
-- One row per (from_type, to_type, rel_rot, local delta, link) with
-- counts. Deltas are expressed in the FROM block's local frame
-- (world delta rotated by -from_rot) so evidence is rotation-invariant,
-- matching the grammar's convention. rel_rot = (to_rot - from_rot) % 4.
--
-- link distinguishes how the route got there:
--   contact  consecutive block visits, no gap between
--   jump     an AIRBORNE stretch between the two visits
--   gap      an OFF_SURFACE stretch between them (terrain, zone
--            interior, item surface); still a real driven transition,
--            but not evidence the blocks join
--
-- Free-placed endpoints carry rel_rot NULL (their rotation is a float
-- yaw, not a quarter-turn).
--
-- Cardinality is small by construction: ~14k block visits across 250
-- captures. This is evidence, not a corpus scan.

CREATE TABLE IF NOT EXISTS driven_transitions (
    id                  BIGINT       NOT NULL AUTO_INCREMENT,
    extractor_version   VARCHAR(32)  NOT NULL,

    from_type           VARCHAR(255) NOT NULL,
    to_type             VARCHAR(255) NOT NULL,
    rel_rot             SMALLINT     NULL,
    dx                  INT          NOT NULL,
    dy                  INT          NOT NULL,
    dz                  INT          NOT NULL,
    link                VARCHAR(16)  NOT NULL,

    n_transitions       INT          NOT NULL,
    n_maps              INT          NOT NULL,

    created_at          DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    created_by_version  VARCHAR(32)  NOT NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uq_dt (extractor_version, from_type, to_type, rel_rot,
                      dx, dy, dz, link),
    KEY ix_dt_from (from_type),
    KEY ix_dt_link (link)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
