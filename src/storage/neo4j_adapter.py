"""Neo4j driver factory + Cypher migration runner.

Mirrors ``storage/mariadb.py`` in spirit. Migration tracking uses a
single ``_Migration`` label; each file applied inserts one node
keyed by filename with a content SHA-256 for edit detection.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import neo4j

from src.utils.config import load_config

_LOG = logging.getLogger(__name__)

_MIGRATION_FILENAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.cypher$")
_DEFAULT_MIGRATIONS_DIR = Path("migrations/neo4j")


class Neo4jMigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CypherMigrationFile:
    path: Path
    filename: str
    content: str

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def open_driver(config: dict[str, Any]) -> neo4j.Driver:
    cfg = config.get("storage", {}).get("neo4j")
    if not isinstance(cfg, dict):
        raise Neo4jMigrationError("config.storage.neo4j is missing or not a mapping")
    return neo4j.GraphDatabase.driver(
        cfg["uri"],
        auth=(cfg["user"], cfg.get("password", "")),
    )


def discover_cypher_migrations(migrations_dir: Path) -> list[CypherMigrationFile]:
    if not migrations_dir.is_dir():
        raise Neo4jMigrationError(f"migrations dir not found: {migrations_dir}")
    out: list[CypherMigrationFile] = []
    for path in sorted(migrations_dir.iterdir()):
        if not path.is_file() or path.suffix != ".cypher":
            continue
        if not _MIGRATION_FILENAME_RE.match(path.name):
            raise Neo4jMigrationError(
                f"cypher migration filename {path.name!r} does not match NNN_name.cypher"
            )
        out.append(
            CypherMigrationFile(path=path, filename=path.name, content=path.read_text(encoding="utf-8"))
        )
    return out


def _split_statements(text: str) -> list[str]:
    """Split Cypher file on ``;`` after stripping ``//`` line comments."""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        # Trailing inline // comment: drop the rest of the line.
        if "//" in line:
            line = line.split("//", 1)[0]
        lines.append(line)
    joined = "\n".join(lines)
    return [stmt.strip() for stmt in joined.split(";") if stmt.strip()]


def _ensure_migration_label(driver: neo4j.Driver) -> None:
    with driver.session() as session:
        session.run(
            "CREATE CONSTRAINT __migration_filename IF NOT EXISTS "
            "FOR (m:_Migration) REQUIRE m.filename IS UNIQUE"
        ).consume()


def _applied_filenames(driver: neo4j.Driver) -> dict[str, str]:
    with driver.session() as session:
        result = session.run("MATCH (m:_Migration) RETURN m.filename AS f, m.content_sha256 AS h")
        return {r["f"]: r["h"] for r in result}


def _record_applied(driver: neo4j.Driver, migration: CypherMigrationFile) -> None:
    with driver.session() as session:
        session.run(
            "CREATE (m:_Migration {filename: $f, content_sha256: $h, applied_at: datetime()})",
            f=migration.filename,
            h=migration.content_sha256,
        ).consume()


def _apply_one(driver: neo4j.Driver, migration: CypherMigrationFile) -> None:
    statements = _split_statements(migration.content)
    if not statements:
        raise Neo4jMigrationError(f"migration {migration.filename} contains no statements")
    _LOG.info("applying %s (%d statements)", migration.filename, len(statements))
    with driver.session() as session:
        for stmt in statements:
            session.run(stmt).consume()


def apply_pending(
    driver: neo4j.Driver, migrations_dir: Path = _DEFAULT_MIGRATIONS_DIR
) -> list[str]:
    migrations = discover_cypher_migrations(migrations_dir)
    if not migrations:
        _LOG.warning("no cypher migration files found in %s", migrations_dir)
        return []

    _ensure_migration_label(driver)
    applied = _applied_filenames(driver)

    newly_applied: list[str] = []
    for migration in migrations:
        if migration.filename in applied:
            stored = applied[migration.filename]
            if stored != migration.content_sha256:
                raise Neo4jMigrationError(
                    f"migration {migration.filename} was modified after apply "
                    f"(stored hash {stored[:12]}, file hash {migration.content_sha256[:12]})"
                )
            continue
        _apply_one(driver, migration)
        _record_applied(driver, migration)
        newly_applied.append(migration.filename)
    return newly_applied


def migrate(
    *, config_path: Path | None = None, migrations_dir: Path = _DEFAULT_MIGRATIONS_DIR
) -> list[str]:
    config = load_config(config_path)
    driver = open_driver(config)
    try:
        return apply_pending(driver, migrations_dir=migrations_dir)
    finally:
        driver.close()


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="src.storage.neo4j_adapter")
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("migrate", help="Apply pending Neo4j Cypher migrations")
    m.add_argument("--config", type=Path, default=None)
    m.add_argument("--migrations-dir", type=Path, default=_DEFAULT_MIGRATIONS_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.command == "migrate":
        try:
            applied = migrate(config_path=args.config, migrations_dir=args.migrations_dir)
        except Neo4jMigrationError as exc:
            _LOG.error("neo4j migration failed: %s", exc)
            return 1
        if applied:
            _LOG.info("applied %d cypher migration(s): %s", len(applied), ", ".join(applied))
        else:
            _LOG.info("neo4j schema already up to date")
        return 0
    return 2
