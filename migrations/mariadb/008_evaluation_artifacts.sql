-- Evaluation artifacts. Shape mirrors src/evaluation/base.py::EvaluationResult.
-- Per-evaluator scores are stored sparsely: any evaluator may emit a subset.

CREATE TABLE IF NOT EXISTS evaluation_artifacts (
    id                      BIGINT        NOT NULL AUTO_INCREMENT,
    map_id                  BIGINT        NOT NULL,
    evaluator_name          VARCHAR(64)   NOT NULL,
    evaluator_version       VARCHAR(32)   NOT NULL,
    benchmark_set_version   VARCHAR(64)   NULL,

    structural_score        DOUBLE        NULL,
    drivability_score       DOUBLE        NULL,
    flow_score              DOUBLE        NULL,
    style_score             DOUBLE        NULL,
    novelty_score           DOUBLE        NULL,
    diversity_metadata      JSON          NULL,
    diagnostics             JSON          NULL,
    notes                   TEXT          NULL,

    created_at              DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    code_version            VARCHAR(64)   NULL,
    source_artifact_ids     JSON          NOT NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uq_eval_artifacts
        (map_id, evaluator_name, evaluator_version, benchmark_set_version),
    KEY ix_eval_artifacts_evaluator (evaluator_name, evaluator_version),
    KEY ix_eval_artifacts_benchmark (benchmark_set_version),

    CONSTRAINT fk_eval_artifacts_map
        FOREIGN KEY (map_id) REFERENCES maps (id)
        ON DELETE CASCADE
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;
