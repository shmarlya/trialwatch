-- migrate:up

-- Where a trial runs, as each source says: one row per trial site (the site's organisation, city,
-- postcode and country) and one per country with no site listed, each with the trial's status in that
-- country. For finding trials a person could take part in, by country and city. Read from the
-- source's record at each transfer (the adapter's `locations`), replacing that source's rows; not an
-- input of the evidence check, so it is not part of a record's `registered` reading and changing how
-- it is read stores no new record version. People at a site (investigators, their emails and phones)
-- are never stored here.
SET LOCAL trialwatch.actor = '1';

CREATE TABLE core.trial_location (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trial_id bigint NOT NULL REFERENCES core.trial (id),
    source_id smallint NOT NULL REFERENCES core.source (id),
    country text NOT NULL CHECK (btrim(country) <> ''),
    city text,
    postcode text,
    -- the site's organisation (a hospital, a clinic); NULL for a country with no site listed
    site text,
    -- the trial's status in this country, as the source gives it (e.g. Ongoing, recruiting; Ended)
    status text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by bigint NOT NULL REFERENCES core.actor (id)
);
COMMENT ON TABLE core.trial_location IS 'where a trial runs, per source: its sites (city, postcode, country) and countries';
CREATE INDEX trial_location_trial_idx ON core.trial_location (trial_id);
CREATE INDEX trial_location_country_idx ON core.trial_location (lower(country));
CREATE INDEX trial_location_city_idx ON core.trial_location (lower(city));
CREATE TRIGGER trial_location_audit AFTER INSERT OR DELETE OR UPDATE ON core.trial_location
    FOR EACH ROW EXECUTE FUNCTION core.audit('id');


-- migrate:down
SET LOCAL trialwatch.actor = '1';
DROP TABLE core.trial_location;
