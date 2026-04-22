-- Breadcrumb sidecar path + content hash on replays.
--
-- Breadcrumbs are the decoded IInput timeline + checkpoint_times_ms
-- emitted by the wrapper alongside the existing telemetry sidecar
-- (see parsers/gbx-wrapper/ReplayParser.cs and docs/reverse-engineering/
-- tm2020-replay-telemetry-spike.md).
--
-- They live in a separate file because the telemetry schema is strict
-- ("samples must be non-empty"), while a TM2020 replay with no
-- position samples can still carry a rich input timeline. Pairing the
-- two in one file would force us to relax the telemetry schema or
-- populate fake samples — both worse than an extra sidecar.
--
-- Columns mirror raw_artifact_path / raw_artifact_hash in shape.
-- Both NULL when the wrapper hasn't been run yet or the replay
-- produced empty breadcrumbs (pre-start buffer replays, spectator
-- artifacts, etc.).

ALTER TABLE replays
    ADD COLUMN breadcrumbs_path VARCHAR(512) NULL AFTER raw_artifact_hash,
    ADD COLUMN breadcrumbs_hash CHAR(64)    NULL AFTER breadcrumbs_path,
    ADD KEY ix_replays_breadcrumbs_hash (breadcrumbs_hash);
