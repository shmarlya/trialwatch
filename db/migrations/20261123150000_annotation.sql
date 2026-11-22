-- migrate:up

-- AI tags with checked quotes (ADR-008, patient search, phase 2): a model reads a recruiting trial's
-- registry fields (conditions and their translations, titles, objective, inclusion criteria, products)
-- and tags it: plain-language categories, the kind of medicine tested, the trial's purpose, synonyms
-- of its conditions in English and its countries' languages, MeSH concepts from a shortlist, and the
-- exact age limits its inclusion criteria give. Every tag names the field it comes from and quotes it;
-- a tag whose quote is not in that field (as whole words) is dropped, and kept as dropped with why.
-- Tags are shown as AI-made, with the model, the quote and the prompt.
--
-- search.annotation_run is one run of the annotate step (like checks.run): the model, its effort and
-- the exact prompt text and sha256 it sent (one current prompt, ADR-004: no versions before production).
-- search.annotation: one trial's answer in a run, append-only; the newest for the trial's current
-- profile is what its search document uses. Billed calls are core.api_call rows (operation
-- 'annotate'), priced like every call, and automatic runs are held by the monthly spending cap.
SET LOCAL trialwatch.actor = '1';

CREATE TABLE search.annotation_run (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model text NOT NULL,
    effort text NOT NULL,
    prompt text NOT NULL,
    prompt_sha256 bytea NOT NULL CHECK (length(prompt_sha256) = 32),
    prompt_text text NOT NULL,
    params jsonb NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed', 'stopped')),
    error text,
    trials integer NOT NULL DEFAULT 0,
    input_tokens bigint NOT NULL DEFAULT 0,
    output_tokens bigint NOT NULL DEFAULT 0,
    cost_usd numeric(14, 6) NOT NULL DEFAULT 0,
    -- the job that ran it (jobs.job), when a worker did
    job_id bigint DEFAULT nullif(current_setting('trialwatch.job', true), '')::bigint REFERENCES jobs.job (id),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    created_by bigint NOT NULL REFERENCES core.actor (id)
);
COMMENT ON TABLE search.annotation_run IS 'a run of AI tagging of recruiting trials, with the exact prompt it sent';
CREATE TRIGGER annotation_run_audit AFTER INSERT OR DELETE OR UPDATE ON search.annotation_run
    FOR EACH ROW EXECUTE FUNCTION core.audit('id', 'prompt_text');

CREATE TABLE search.annotation (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id bigint NOT NULL REFERENCES search.annotation_run (id),
    trial_id bigint NOT NULL REFERENCES core.trial (id),
    -- the profile it read: an annotation of another profile is not used
    profile_hash bytea NOT NULL CHECK (length(profile_hash) = 32),
    served_by text,
    -- the tags whose quotes were found in their field, and the ones dropped, each with why
    facts jsonb NOT NULL DEFAULT '{}',
    dropped jsonb NOT NULL DEFAULT '[]',
    -- the request as sent, without the system prompt (kept once with the run), and the answer as given
    request jsonb,
    raw_answer text,
    error text,
    input_tokens integer NOT NULL DEFAULT 0,
    output_tokens integer NOT NULL DEFAULT 0,
    cost_usd numeric(14, 6),
    duration_ms integer,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id, trial_id)
);
ALTER TABLE search.annotation ALTER COLUMN request SET COMPRESSION lz4;
COMMENT ON TABLE search.annotation IS 'one trial''s AI tags in a run: kept facts with their quotes, dropped ones with why';
CREATE INDEX annotation_trial_idx ON search.annotation (trial_id, profile_hash, id DESC);

ALTER TABLE core.api_call ADD COLUMN annotation_run_id bigint REFERENCES search.annotation_run (id);

ALTER TABLE search.document
    ADD COLUMN annotation_id bigint REFERENCES search.annotation (id),
    -- the exact age limits the inclusion criteria give (AI, with a checked quote), in years
    ADD COLUMN age_min numeric,
    ADD COLUMN age_max numeric,
    -- the kind of medicine tested and the trial's purpose (AI, with a checked quote)
    ADD COLUMN interventions text[] NOT NULL DEFAULT '{}',
    ADD COLUMN purposes text[] NOT NULL DEFAULT '{}';

ALTER TABLE jobs.job DROP CONSTRAINT job_kind_check;
ALTER TABLE jobs.job ADD CONSTRAINT job_kind_check CHECK (kind IN ('sync', 'transfer', 'check', 'refresh', 'annotate'));
ALTER TABLE jobs.schedule DROP CONSTRAINT schedule_kind_check;
ALTER TABLE jobs.schedule ADD CONSTRAINT schedule_kind_check
    CHECK (kind IN ('sync', 'transfer', 'check', 'refresh', 'annotate'));


-- migrate:down
SET LOCAL trialwatch.actor = '1';
ALTER TABLE jobs.schedule DROP CONSTRAINT schedule_kind_check;
ALTER TABLE jobs.schedule ADD CONSTRAINT schedule_kind_check CHECK (kind IN ('sync', 'transfer', 'check', 'refresh'));
ALTER TABLE jobs.job DROP CONSTRAINT job_kind_check;
ALTER TABLE jobs.job ADD CONSTRAINT job_kind_check CHECK (kind IN ('sync', 'transfer', 'check', 'refresh'));
ALTER TABLE search.document DROP COLUMN annotation_id, DROP COLUMN age_min, DROP COLUMN age_max,
    DROP COLUMN interventions, DROP COLUMN purposes;
ALTER TABLE core.api_call DROP COLUMN annotation_run_id;
DROP TABLE search.annotation;
DROP TABLE search.annotation_run;
