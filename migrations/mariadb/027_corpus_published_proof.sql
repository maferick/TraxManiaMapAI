-- Phase 2 — corpus-finishable axiom (docs/learning/corpus-finishable-axiom.md).
--
-- Adds a 'corpus_published' tier to map_finishability_proof.proof_source
-- that sits BETWEEN 'internal_route' and 'none' on the precedence ladder.
--
-- The axiom: every map ingested into our corpus came off TMX, was
-- downloaded by us, parsed cleanly, and (by virtue of being on TMX in
-- the first place) loaded + driven in someone's TM2020 install. That's
-- weak evidence of finishability — strictly weaker than a clean replay
-- or an author-set time, but strictly STRONGER than 'none', which used
-- to swallow this entire population.
--
-- Why the new tier matters in practice: under the v0.5 scheme a corpus
-- map with no replays, no author time, and no internal-route gate
-- result was indistinguishable in the dashboards from a never-ingested
-- map. Generation, learning, and operator triage all conflated the two.
-- The new tier separates "we have nothing" from "we have the map and
-- nothing else."
--
-- Renderer mapping (frontend changes follow in a separate PR):
--   replay            → "Player validated"
--   author_time       → "Author validated"
--   world_record      → "Player validated (unverified)"
--   internal_route    → "Internally verified"
--   corpus_published  → "Published map (axiom)"        ← new
--   none              → (no badge)

ALTER TABLE map_finishability_proof
    MODIFY COLUMN proof_source ENUM(
        'replay',
        'author_time',
        'world_record',
        'internal_route',
        'corpus_published',
        'none'
    ) NOT NULL DEFAULT 'corpus_published';
