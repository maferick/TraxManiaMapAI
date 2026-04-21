from __future__ import annotations

from pathlib import Path

import pytest

from src.storage.mariadb import (
    MigrationError,
    MigrationFile,
    _split_statements,
    discover_migrations,
)


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_discover_orders_lexicographically(tmp_path: Path) -> None:
    _write(tmp_path / "002_b.sql", "CREATE TABLE b (id INT);")
    _write(tmp_path / "000_a.sql", "CREATE TABLE a (id INT);")
    _write(tmp_path / "001_c.sql", "CREATE TABLE c (id INT);")
    migrations = discover_migrations(tmp_path)
    assert [m.filename for m in migrations] == [
        "000_a.sql",
        "001_c.sql",
        "002_b.sql",
    ]


def test_discover_rejects_bad_filename(tmp_path: Path) -> None:
    _write(tmp_path / "not_a_migration.sql", "-- oops")
    with pytest.raises(MigrationError, match="NNN_name"):
        discover_migrations(tmp_path)


def test_discover_ignores_non_sql_files(tmp_path: Path) -> None:
    _write(tmp_path / "000_bootstrap.sql", "CREATE TABLE x (id INT);")
    _write(tmp_path / "README.md", "readme")
    _write(tmp_path / "notes.txt", "notes")
    migrations = discover_migrations(tmp_path)
    assert [m.filename for m in migrations] == ["000_bootstrap.sql"]


def test_content_sha256_is_stable(tmp_path: Path) -> None:
    path = tmp_path / "000_bootstrap.sql"
    _write(path, "CREATE TABLE x (id INT);\n")
    m1 = MigrationFile(path=path, filename=path.name, content=path.read_text())
    m2 = MigrationFile(path=path, filename=path.name, content=path.read_text())
    assert m1.content_sha256 == m2.content_sha256


def test_split_statements_strips_comments() -> None:
    sql = """
    -- leading comment
    CREATE TABLE a (id INT);
    -- another comment
    CREATE TABLE b (id INT);
    """
    assert _split_statements(sql) == ["CREATE TABLE a (id INT)", "CREATE TABLE b (id INT)"]


def test_split_statements_filters_empty() -> None:
    assert _split_statements(";;  ;\n -- only comments\n;") == []


def test_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="not found"):
        discover_migrations(tmp_path / "does_not_exist")


def test_repo_migrations_parse_cleanly() -> None:
    """The checked-in migrations must satisfy the runner's rules."""
    migrations = discover_migrations(Path("migrations/mariadb"))
    names = [m.filename for m in migrations]
    assert names[0] == "000_bootstrap.sql"
    assert all(len(_split_statements(m.content)) >= 1 for m in migrations)
