-- migrate:up

-- A profile any source can fill (ADR-008, patient search). Search compares a site's country with a
-- trial's recruiting countries by ISO 3166 code (src/trialwatch/geo/countries.py), so two registries
-- spelling a country differently still meet. A source that publishes where a site is
-- (ClinicalTrials.gov's geoPoint) gives its point; other sites are found in GeoNames (geo.address_point)
-- as before. Both are written by every transfer and by `trialwatch source locations`.
SET LOCAL trialwatch.actor = '1';

ALTER TABLE core.trial_location
    ADD COLUMN country_iso2 char(2),
    ADD COLUMN latitude double precision,
    ADD COLUMN longitude double precision,
    ADD CONSTRAINT trial_location_point_check CHECK ((latitude IS NULL) = (longitude IS NULL));
COMMENT ON COLUMN core.trial_location.country_iso2 IS
    'the country''s ISO 3166 code (geo/countries.py); NULL: a name we don''t know (no part in finding trials by country)';
COMMENT ON COLUMN core.trial_location.latitude IS 'where the site is, when the source says so; else geo.address_point by its address';
CREATE INDEX trial_location_iso2_idx ON core.trial_location (trial_id, country_iso2);

-- A source may code a trial's conditions in MeSH itself (ClinicalTrials.gov does): such a concept is
-- taken as it is (method source-code), with nothing to look for
ALTER TABLE search.trial_concept DROP CONSTRAINT trial_concept_method_check,
    ADD CONSTRAINT trial_concept_method_check
        CHECK (method IN ('exact-name', 'same-words', 'phrase', 'source-code', 'ai-quote'));


-- The countries a query's place is looked for in are the ones `trialwatch geo load` loaded
-- (TRIALWATCH_COUNTRIES, by default the EU/EEA, where CTIS trials have sites), not a fixed EU/EEA list
ALTER TABLE geo.country RENAME COLUMN eea TO in_gazetteer;
COMMENT ON COLUMN geo.country.in_gazetteer IS 'its places and postal codes are loaded: a query''s place is looked for here';


-- migrate:down
SET LOCAL trialwatch.actor = '1';
ALTER TABLE geo.country RENAME COLUMN in_gazetteer TO eea;
COMMENT ON COLUMN geo.country.eea IS NULL;
ALTER TABLE search.trial_concept DROP CONSTRAINT trial_concept_method_check,
    ADD CONSTRAINT trial_concept_method_check CHECK (method IN ('exact-name', 'same-words', 'phrase', 'ai-quote'));
DROP INDEX core.trial_location_iso2_idx;
ALTER TABLE core.trial_location DROP CONSTRAINT trial_location_point_check, DROP COLUMN country_iso2,
    DROP COLUMN latitude, DROP COLUMN longitude;
