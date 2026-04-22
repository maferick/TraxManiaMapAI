-- Per-map traversability edge evidence. Materializes what
-- src/corridor/traversability/reachability.py + enumeration.py compute
-- on-the-fly so downstream code (evaluators, future corridor artifact
-- generation, cross-map analysis) can query it without re-running
-- classification + graph construction every time.
--
-- Schema follows docs/workstreams/corridor-prereq-2-traversability.md
-- §6.1. map_id is load-bearing — traversability is evaluated per-map
-- because the same (block_family_A, block_family_B) pair in different
-- map contexts may have different states once per-map signals enter.
--
-- The four signal columns (path_support_count, pattern_weight,
-- negative_evidence_count) are written by different parts of the
-- pipeline at different times; each is a separate write path, so
-- they don't NEED to be populated together. v0.1 only writes
-- rule_support + traversability_state; the signal columns default to
-- 0 and get updated in later phases as those signals are implemented.
--
-- classification_version is the
-- src.corridor.traversability.classification.CLASSIFICATION_VERSION
-- at write time. A bump to that value INVALIDATES existing rows for
-- the bumped version — callers must either query by the current
-- version or re-run the build to migrate. Per-version coexistence
-- is supported via the UNIQUE KEY.
--
-- Row volume at 2026-04-scale-1k scale (999 parsed maps): ~4M rows
-- (each map averages ~4500 axis-neighbor edges). Comfortable for
-- MariaDB; the per-map index keeps per-map lookups under 10ms.

CREATE TABLE traversability_edge_evidence (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    map_id BIGINT NOT NULL,
    src_block_id BIGINT NOT NULL,
    dst_block_id BIGINT NOT NULL,
    traversability_state ENUM(
        'seed_valid', 'supported', 'unsupported', 'unknown'
    ) NOT NULL,
    rule_support TINYINT(1) NOT NULL DEFAULT 0,
    path_support_count INT NOT NULL DEFAULT 0,
    pattern_weight DOUBLE NOT NULL DEFAULT 0.0,
    negative_evidence_count INT NOT NULL DEFAULT 0,
    classification_version VARCHAR(32) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_trv_ev_map
        FOREIGN KEY (map_id) REFERENCES maps (id) ON DELETE CASCADE,
    CONSTRAINT fk_trv_ev_src
        FOREIGN KEY (src_block_id) REFERENCES block_placements (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_trv_ev_dst
        FOREIGN KEY (dst_block_id) REFERENCES block_placements (id)
        ON DELETE CASCADE,
    -- One evidence row per (map, ordered edge, classification_version).
    -- Different classification versions coexist so a re-classification
    -- bump can land without blowing away the old rows until migration
    -- is complete.
    UNIQUE KEY uq_trv_ev_edge
        (map_id, src_block_id, dst_block_id, classification_version),
    KEY ix_trv_ev_map_state (map_id, traversability_state),
    KEY ix_trv_ev_state (traversability_state)
);
