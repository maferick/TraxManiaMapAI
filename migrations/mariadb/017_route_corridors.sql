-- Enumerated corridor-candidate paths per (map, interval). Each row
-- is one simple path of grid cells from a spawn anchor set to a
-- non-spawn anchor set, in the order produced by the §8.4 depth-10
-- DFS enumeration.
--
-- Consumers: corridor-ranking code (future), the evaluator that
-- surfaces corridor confidence in the PR 7 dry-run (future), and
-- downstream replay-to-corridor matching (post-OpenPlanet
-- telemetry). Today it's the canonical storage for what
-- enumerate_map produces on-the-fly.
--
-- path_rank orders paths within one interval: rank 0 is the top
-- path (shortest by cell count, ties broken lexicographically by
-- cell tuple — matches _top_ranked_path in enumeration.py so the
-- §8.3.4 stability check sees the same ordering consumers see).
--
-- top_n persistence cap: design note §8.4 already bounds enumeration
-- at depth-10 + 10,000 path hard-cap per interval. Storing all
-- of them is feasible on the scale-1k corpus but not all consumers
-- need them — the build pipeline takes a top_n flag so callers can
-- keep only the first N per interval. Schema doesn't enforce top_n
-- because a different consumer may want all paths; UNIQUE on
-- (map, interval, path_rank, version) just ensures per-(map,
-- interval) ranks are unique.
--
-- contains_virtual_edge flags paths that traversed a replay-
-- observation virtual edge. Consumers that need "real grid path
-- only" (e.g. for edge-level evidence writes) filter to
-- contains_virtual_edge = 0.
--
-- Row volume projection on scale-1k with top_n=100 default:
-- ~1000 maps × ~5 intervals × 100 paths ≈ 500k rows. Comfortable.

CREATE TABLE route_corridors (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    map_id BIGINT NOT NULL,
    src_tag VARCHAR(32) NOT NULL,
    src_order INT NOT NULL,
    dst_tag VARCHAR(32) NOT NULL,
    dst_order INT NOT NULL,
    path_rank INT NOT NULL,
    path_cells LONGTEXT NOT NULL,
    path_length INT NOT NULL,
    contains_virtual_edge TINYINT(1) NOT NULL DEFAULT 0,
    classification_version VARCHAR(32) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_rc_map
        FOREIGN KEY (map_id) REFERENCES maps (id) ON DELETE CASCADE,
    UNIQUE KEY uq_rc_slot (
        map_id, src_tag, src_order, dst_tag, dst_order,
        path_rank, classification_version
    ),
    KEY ix_rc_map (map_id, classification_version),
    KEY ix_rc_interval (
        map_id, src_tag, dst_tag, classification_version, path_rank
    )
);
