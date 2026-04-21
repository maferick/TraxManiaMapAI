// Block node identity. Every block is uniquely keyed by a normalized
// composite string of (family, type, variant). Nulls are normalized to
// empty strings at write time so the constraint always applies.

CREATE CONSTRAINT block_key IF NOT EXISTS
FOR (b:Block) REQUIRE b.key IS UNIQUE;

CREATE INDEX block_family_type IF NOT EXISTS
FOR (b:Block) ON (b.family, b.type);
