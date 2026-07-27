-- OpenPlanet telemetry sidecar path + content hash on replays.
--
-- The offline GBX path cannot decode TM2020 position samples
-- (RaceValidateGhost reports samples = 0 for every map in the corpus),
-- so trajectories come from playing the author's ghost in-game and
-- sampling it: see openplanet-plugin/AIReplayTelemetry,
-- tools/capture_replay_telemetry.py and
-- docs/workstreams/openplanet-telemetry.md.
--
-- Separate from breadcrumbs_path on purpose. Breadcrumbs stay the
-- wrapper's responsibility and carry a decoded input timeline with no
-- positions; this sidecar carries positions with no inputs, because a
-- ghost exposes no input surface through any typed game API. Neither
-- supersedes the other and a replay can legitimately have both, one or
-- none.
--
-- Columns mirror breadcrumbs_path / breadcrumbs_hash in shape.
--
-- capture_source records WHICH ghost was driven. The author's
-- validation ghost is a guaranteed successful finish and is what the
-- gold-set work uses; a leaderboard replay or one of our own runs is
-- not equivalent evidence, and conflating them would silently mix
-- populations in any model trained downstream.

ALTER TABLE replays
    ADD COLUMN openplanet_telemetry_path VARCHAR(512) NULL
        AFTER breadcrumbs_hash,
    ADD COLUMN openplanet_telemetry_hash CHAR(64) NULL
        AFTER openplanet_telemetry_path,
    ADD COLUMN capture_source VARCHAR(32) NULL
        AFTER openplanet_telemetry_hash,
    ADD KEY ix_replays_openplanet_telemetry_hash
        (openplanet_telemetry_hash);
