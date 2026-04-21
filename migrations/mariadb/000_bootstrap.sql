-- Schema-migration tracking. Must be first. Every subsequent migration
-- writes a row here on successful apply.

CREATE TABLE IF NOT EXISTS schema_migrations (
    id              BIGINT       NOT NULL AUTO_INCREMENT,
    filename        VARCHAR(255) NOT NULL,
    content_sha256  CHAR(64)     NOT NULL,
    applied_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_schema_migrations_filename (filename)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;
