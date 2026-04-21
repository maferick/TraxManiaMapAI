// Internal tracking label for applied migrations. Must run first; the
// adapter creates the same constraint defensively before any migration
// runs, so re-applying this file is a no-op.

CREATE CONSTRAINT __migration_filename IF NOT EXISTS
FOR (m:_Migration) REQUIRE m.filename IS UNIQUE;
