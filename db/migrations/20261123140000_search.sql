-- migrate:up

-- The search index (ADR-008, patient search): one search document per trial, built from its profile
-- (core.trial_profile) at every transfer and by `trialwatch search reindex`, so a trial is findable the
-- moment it arrives. Its words carry a weight for where they came from:
--   A  conditions, their translations, the MedDRA terms CTIS publishes, the trial's MeSH concepts
--   B  titles and their translations, lay categories, the concepts above the trial's (MeSH hierarchy)
--   C  what is tested (products, substances, ATC names) and the main objective
--   D  inclusion criteria
-- Exclusion criteria are never indexed: "patients with diabetes are excluded" must not match
-- "diabetes". Words are compared without accents (search.trialwatch: simple + unaccent), so "diabète"
-- and "diabete" meet; no stemming, which differs by language: a search matches word beginnings.
--
-- search.trial_concept: which MeSH descriptors a trial's conditions name, how that was found, and the
-- words it was found in (field and quote), so every concept shown can be checked against the registry.
-- search.term: the words of recruiting trials' conditions and titles, for "did you mean".
-- All three are derived and rebuilt from stored data: not audited.
SET LOCAL trialwatch.actor = '1';

CREATE SCHEMA search;

CREATE TEXT SEARCH CONFIGURATION search.trialwatch (COPY = pg_catalog.simple);
ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ALTER MAPPING FOR asciiword, asciihword, hword_asciipart, word, hword, hword_part WITH public.unaccent, simple;

CREATE TABLE search.document (
    trial_id bigint PRIMARY KEY REFERENCES core.trial (id),
    source_id smallint NOT NULL REFERENCES core.source (id),
    -- the profile it was built from: the same hash again needs no rebuilding
    profile_hash bytea NOT NULL,
    recruiting boolean NOT NULL,
    -- countries where it recruits now, as the source names them (core.trial_location.country)
    recruiting_countries text[] NOT NULL DEFAULT '{}',
    title text,
    conditions text[] NOT NULL DEFAULT '{}',
    -- lay categories (src/trialwatch/search/categories.json)
    categories text[] NOT NULL DEFAULT '{}',
    phase text,
    age_groups text[] NOT NULL DEFAULT '{}',
    female boolean,
    male boolean,
    healthy_volunteers boolean,
    patients boolean,
    rare boolean NOT NULL DEFAULT false,
    tsv tsvector NOT NULL,
    built_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
COMMENT ON TABLE search.document IS 'one per trial, built from its profile: what patient search matches (ADR-008)';
CREATE INDEX document_tsv_idx ON search.document USING gin (tsv);
CREATE INDEX document_recruiting_idx ON search.document (trial_id) WHERE recruiting;

CREATE TABLE search.trial_concept (
    trial_id bigint NOT NULL REFERENCES core.trial (id),
    ui text NOT NULL,
    -- exact-name | same-words | phrase (found in the text without AI) | ai-quote (Phase 2)
    method text NOT NULL CHECK (method IN ('exact-name', 'same-words', 'phrase', 'ai-quote')),
    -- which text of the profile it was found in (condition-1, meddra-2) and the words found
    field text NOT NULL,
    quote text NOT NULL,
    PRIMARY KEY (trial_id, ui, field)
);
COMMENT ON TABLE search.trial_concept IS 'the MeSH descriptors a trial''s conditions name, with the words they were found in';

CREATE TABLE search.term (
    term text PRIMARY KEY,
    documents integer NOT NULL
);
COMMENT ON TABLE search.term IS 'words of recruiting trials'' conditions and titles, for "did you mean"';
CREATE INDEX term_trgm_idx ON search.term USING gin (term gin_trgm_ops);


-- migrate:down
SET LOCAL trialwatch.actor = '1';
DROP SCHEMA search CASCADE;
