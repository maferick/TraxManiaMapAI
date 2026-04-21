"""Storage adapters.

MariaDB: migrations + connection factory (PR 3).
Neo4j: migrations + driver factory (PR 6).
"""
from .mariadb import MigrationError, apply_pending, migrate, open_connection
from .neo4j_adapter import (
    Neo4jMigrationError,
    apply_pending as apply_neo4j_pending,
    migrate as neo4j_migrate,
    open_driver,
)

__all__ = [
    "MigrationError",
    "Neo4jMigrationError",
    "apply_neo4j_pending",
    "apply_pending",
    "migrate",
    "neo4j_migrate",
    "open_connection",
    "open_driver",
]
