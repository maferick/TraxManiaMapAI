"""MariaDB connection factory + migration runner.

The runner applies ordered SQL files from ``migrations/mariadb/`` and
tracks applied files in the ``schema_migrations`` table. Re-applying
a migration is a no-op; editing an already-applied migration is a
hard error (the recorded content hash catches it).
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pymysql
from pymysql.connections import Connection

from src.utils.config import load_config

_LOG = logging.getLogger(__name__)

_MIGRATION_FILENAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")
_DEFAULT_MIGRATIONS_DIR = Path("migrations/mariadb")
_SCHEMA_MIGRATIONS_TABLE = "schema_migrations"


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationFile:
    path: Path
    filename: str
    content: str

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def open_connection(config: dict[str, Any]) -> Connection:
    """Open a PyMySQL connection using the resolved config dict."""
    cfg = config.get("storage", {}).get("mariadb")
    if not isinstance(cfg, dict):
        raise MigrationError("config.storage.mariadb is missing or not a mapping")
    return pymysql.connect(
        host=cfg["host"],
        port=int(cfg.get("port", 3306)),
        user=cfg["user"],
        password=cfg.get("password", ""),
        database=cfg["database"],
        charset="utf8mb4",
        autocommit=False,
    )


@contextmanager
def cursor(conn: Connection) -> Iterator[Any]:
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


def discover_migrations(migrations_dir: Path) -> list[MigrationFile]:
    if not migrations_dir.is_dir():
        raise MigrationError(f"migrations dir not found: {migrations_dir}")
    files: list[MigrationFile] = []
    for path in sorted(migrations_dir.iterdir()):
        if not path.is_file() or path.suffix != ".sql":
            continue
        if not _MIGRATION_FILENAME_RE.match(path.name):
            raise MigrationError(
                f"migration filename {path.name!r} does not match NNN_name.sql"
            )
        files.append(
            MigrationFile(path=path, filename=path.name, content=path.read_text(encoding="utf-8"))
        )
    return files


def _split_statements(sql: str) -> list[str]:
    lines: list[str] = []
    for line in sql.splitlines():
        if line.lstrip().startswith("--"):
            continue
        lines.append(line)
    joined = "\n".join(lines)
    return [stmt.strip() for stmt in joined.split(";") if stmt.strip()]


def _schema_migrations_exists(conn: Connection) -> bool:
    with cursor(conn) as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name = %s
            LIMIT 1
            """,
            (_SCHEMA_MIGRATIONS_TABLE,),
        )
        return cur.fetchone() is not None


def _applied_migrations(conn: Connection) -> dict[str, str]:
    with cursor(conn) as cur:
        cur.execute(
            f"SELECT filename, content_sha256 FROM {_SCHEMA_MIGRATIONS_TABLE}"
        )
        rows = cur.fetchall()
    return {row[0]: row[1] for row in rows}


def _record_applied(conn: Connection, migration: MigrationFile) -> None:
    with cursor(conn) as cur:
        cur.execute(
            f"INSERT INTO {_SCHEMA_MIGRATIONS_TABLE} (filename, content_sha256) VALUES (%s, %s)",
            (migration.filename, migration.content_sha256),
        )


def _apply_one(conn: Connection, migration: MigrationFile) -> None:
    statements = _split_statements(migration.content)
    if not statements:
        raise MigrationError(f"migration {migration.filename} contains no statements")
    _LOG.info("applying %s (%d statements)", migration.filename, len(statements))
    try:
        with cursor(conn) as cur:
            for stmt in statements:
                cur.execute(stmt)
    except pymysql.MySQLError as exc:
        conn.rollback()
        raise MigrationError(f"failed applying {migration.filename}: {exc}") from exc


def apply_pending(
    conn: Connection,
    migrations_dir: Path = _DEFAULT_MIGRATIONS_DIR,
) -> list[str]:
    """Apply unapplied migrations in order. Returns the filenames applied."""
    migrations = discover_migrations(migrations_dir)
    if not migrations:
        _LOG.warning("no migration files found in %s", migrations_dir)
        return []

    applied_new: list[str] = []

    if not _schema_migrations_exists(conn):
        bootstrap = migrations[0]
        if bootstrap.filename != "000_bootstrap.sql":
            raise MigrationError(
                f"first migration must be 000_bootstrap.sql, got {bootstrap.filename}"
            )
        _apply_one(conn, bootstrap)
        _record_applied(conn, bootstrap)
        conn.commit()
        applied_new.append(bootstrap.filename)
        remaining = migrations[1:]
    else:
        remaining = migrations

    applied = _applied_migrations(conn)
    for migration in remaining:
        if migration.filename in applied:
            stored_hash = applied[migration.filename]
            if stored_hash != migration.content_sha256:
                raise MigrationError(
                    f"migration {migration.filename} was modified after apply "
                    f"(stored hash {stored_hash[:12]}, file hash {migration.content_sha256[:12]})"
                )
            continue
        _apply_one(conn, migration)
        _record_applied(conn, migration)
        conn.commit()
        applied_new.append(migration.filename)

    return applied_new


def migrate(
    *,
    config_path: Path | None = None,
    migrations_dir: Path = _DEFAULT_MIGRATIONS_DIR,
) -> list[str]:
    config = load_config(config_path)
    conn = open_connection(config)
    try:
        return apply_pending(conn, migrations_dir=migrations_dir)
    finally:
        conn.close()


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="src.storage.mariadb")
    sub = parser.add_subparsers(dest="command", required=True)

    migrate_cmd = sub.add_parser("migrate", help="Apply pending migrations")
    migrate_cmd.add_argument("--config", type=Path, default=None)
    migrate_cmd.add_argument("--migrations-dir", type=Path, default=_DEFAULT_MIGRATIONS_DIR)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "migrate":
        try:
            applied = migrate(config_path=args.config, migrations_dir=args.migrations_dir)
        except MigrationError as exc:
            _LOG.error("migration failed: %s", exc)
            return 1
        if applied:
            _LOG.info("applied %d migration(s): %s", len(applied), ", ".join(applied))
        else:
            _LOG.info("schema already up to date")
        return 0
    return 2
