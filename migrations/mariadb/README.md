# migrations/mariadb

Ordered, forward-only SQL migrations for MariaDB 11.

## Naming

`NNN_short_name.sql` where `NNN` is a three-digit zero-padded integer.
Files are applied in lexicographic order. Applied migrations are
tracked in the `schema_migrations` table (created by `000_bootstrap.sql`)
and a file cannot be re-applied or silently re-edited.

## Rules

- **Forward-only.** No down-migrations. If a change needs to be undone,
  write a new migration that does the reverse.
- **Edit-once.** After a migration has been applied in any environment,
  editing its contents is a bug. The migration runner records the
  content hash and will abort if the hash changes.
- **Additive first.** Prefer adding new columns over changing existing
  ones. When a change is destructive (drop column, change type), call
  it out in a comment at the top of the file.
- **InnoDB + utf8mb4.** Every table uses InnoDB engine and
  `utf8mb4_unicode_ci` collation.

## Applying migrations

```bash
python -m src.storage.mariadb migrate
```

Reads connection details from `config/settings.yaml` under
`storage.mariadb`. Idempotent — re-running does nothing if everything
is already applied.

## Enum evolution

ENUM columns encode closed taxonomies. Adding a value is a migration;
removing one is a migration with a data audit. Don't change an ENUM
value in place — rename is a column swap + backfill.
