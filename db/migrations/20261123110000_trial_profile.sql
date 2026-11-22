-- migrate:up

-- A trial's profile (ADR-008, patient search): what someone looking for a trial to join needs, read by
-- a source's adapter from the trial's record into one shape for every source
-- (src/trialwatch/sources/profile.py): conditions with their translations and the coded terms the
-- source publishes (MedDRA), therapeutic areas (MeSH tree branches), phase, who can join (age groups,
-- sex, patients or healthy volunteers, the criteria as written), what is tested, and per country the
-- trial's status and whether it recruits now. Never the people at a site.
--
-- Read at every transfer and replaced when it changed; rebuilt from stored records without fetching
-- (`trialwatch source profiles`). Not part of a record's `registered` reading, so reading it
-- differently stores no new record version and outdates no judgement. The audit log keeps who changed
-- a profile and when, not its (large) content: the record it was read from is kept.
SET LOCAL trialwatch.actor = '1';

CREATE TABLE core.trial_profile (
    trial_id bigint NOT NULL REFERENCES core.trial (id),
    source_id smallint NOT NULL REFERENCES core.source (id),
    -- the record it was read from (NULL: a source with no stored record)
    trial_record_id bigint REFERENCES core.trial_record (id),
    profile jsonb NOT NULL,
    profile_hash bytea NOT NULL CHECK (length(profile_hash) = 32),
    -- the trial recruits now somewhere, as the profile says (the source's list first)
    recruiting boolean NOT NULL,
    read_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by bigint NOT NULL REFERENCES core.actor (id),
    PRIMARY KEY (trial_id, source_id)
);
ALTER TABLE core.trial_profile ALTER COLUMN profile SET COMPRESSION lz4;
COMMENT ON TABLE core.trial_profile IS
    'what a person looking for a trial needs, per trial and source, read from its record (sources/profile.py)';
CREATE INDEX trial_profile_recruiting_idx ON core.trial_profile (trial_id) WHERE recruiting;
CREATE TRIGGER trial_profile_audit AFTER INSERT OR DELETE OR UPDATE ON core.trial_profile
    FOR EACH ROW EXECUTE FUNCTION core.audit('trial_id,source_id', 'profile,read_at');


-- migrate:down
SET LOCAL trialwatch.actor = '1';
DROP TABLE core.trial_profile;
