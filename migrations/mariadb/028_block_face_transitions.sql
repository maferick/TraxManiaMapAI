-- Face-aware block transitions (catalogue era).
--
-- Unlike block_pair_transitions (cell adjacency along replay-backed
-- corridor paths), a row here means: in a corpus map, block A's
-- route-clip port at some face met block B's matching port on the
-- shared cell boundary — the same join relation the game itself uses
-- (clips), computed from the extracted block catalogue. Under the
-- corpus-finishable axiom this is weak-positive, high-coverage
-- evidence available for EVERY parsed map, replays or not.
--
-- Key includes the clip id and the relative rotation (rot_b - rot_a
-- mod 4) so the generator can use it as a directional prior at an
-- open port. Natural composite key overflows the 3072-byte index
-- limit; sha256 signature PK per the block_triple_transitions
-- precedent.

CREATE TABLE IF NOT EXISTS block_face_transitions (
    transition_signature CHAR(64)     NOT NULL,
    block_a              VARCHAR(255) NOT NULL,
    block_b              VARCHAR(255) NOT NULL,
    clip_id              VARCHAR(128) NOT NULL,
    rel_rotation         TINYINT      NOT NULL,
    environment          VARCHAR(64)  NOT NULL DEFAULT '',
    transition_count     BIGINT       NOT NULL DEFAULT 0,
    map_count            BIGINT       NOT NULL DEFAULT 0,
    created_at           DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at           DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                      ON UPDATE CURRENT_TIMESTAMP(6),
    created_by_version   VARCHAR(64)  NOT NULL,
    PRIMARY KEY (transition_signature),
    KEY idx_bft_block_a (block_a, clip_id),
    KEY idx_bft_block_b (block_b, clip_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
