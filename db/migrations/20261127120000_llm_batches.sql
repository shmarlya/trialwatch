-- migrate:up

-- Model calls sent as Anthropic Message Batches (ADR-009): every token at half price, answered within
-- 24 hours. A batch (core.llm_batch) holds one round of a check or tagging run's requests
-- (core.llm_batch_item); each answer becomes a core.llm_call like a direct call's when its trial uses it,
-- priced at batch prices and naming its batch. While its batch runs, a job waits (status waiting) instead of keeping the
-- worker, and so does its run.
SET LOCAL trialwatch.actor = '1';

CREATE TABLE core.llm_batch (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Anthropic's id; NULL while it is being sent
    provider_batch_id  text UNIQUE,
    provider           text NOT NULL DEFAULT 'anthropic',
    -- what it is for: check, annotate
    operation          text NOT NULL,
    job_id             bigint REFERENCES jobs.job DEFAULT nullif(current_setting('trialwatch.job', true), '')::bigint,
    check_run_id       bigint REFERENCES checks.run,
    annotation_run_id  bigint REFERENCES search.annotation_run,
    -- 1: the first calls; 2 and on: widening and requests asked again
    round              integer NOT NULL CHECK (round >= 1),
    -- submitting: being sent; submitted: Anthropic is answering; collected: its results are recorded;
    -- failed: it could not be sent
    status             text NOT NULL DEFAULT 'submitting'
                       CHECK (status IN ('submitting', 'submitted', 'collected', 'failed')),
    request_count      integer NOT NULL CHECK (request_count >= 0),
    -- what it was thought to cost before it was sent (the spending cap is checked against it)
    estimated_usd      numeric(14, 6),
    -- Anthropic's counts when last asked: processing, succeeded, errored, canceled, expired
    counts             jsonb NOT NULL DEFAULT '{}',
    error              text,
    created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    submitted_at       timestamptz,
    ended_at           timestamptz,
    collected_at       timestamptz,
    CHECK (status IN ('submitting', 'failed') OR provider_batch_id IS NOT NULL)
);
COMMENT ON TABLE core.llm_batch IS 'a Message Batch: one round of a run''s model requests, sent together at batch prices';
CREATE INDEX llm_batch_job_idx ON core.llm_batch (job_id);

CREATE TABLE core.llm_batch_item (
    llm_batch_id  bigint NOT NULL REFERENCES core.llm_batch,
    -- the request's id in its batch: the sha256 of the request as sent, so a run asking the same again
    -- finds its answer
    custom_id     text NOT NULL,
    trial_id      bigint NOT NULL REFERENCES core.trial,
    -- its number within the trial's check (1: the first call, 2: widening)
    call_number   integer NOT NULL,
    model         text NOT NULL,
    -- its call once recorded: an answer is recorded once (when its trial used it, or as unused)
    llm_call_id   bigint REFERENCES core.llm_call,
    PRIMARY KEY (llm_batch_id, custom_id)
);
COMMENT ON TABLE core.llm_batch_item IS 'one request of a Message Batch, and its call once answered';

ALTER TABLE core.llm_call ADD COLUMN tier text NOT NULL DEFAULT 'direct' CHECK (tier IN ('direct', 'batch'));
ALTER TABLE core.llm_call ADD COLUMN llm_batch_id bigint REFERENCES core.llm_batch;
ALTER TABLE core.llm_call ADD COLUMN custom_id text;
COMMENT ON COLUMN core.llm_call.tier IS 'direct: sent alone, at list prices; batch: answered in a Message Batch, at batch prices';

ALTER TABLE jobs.job DROP CONSTRAINT job_status_check;
-- queued: waits for a worker (from run_after); running; waiting: on a Message Batch (from wait_until the
-- worker asks Anthropic whether it ended); held: by the spending cap; succeeded / partial / failed /
-- cancelled: ended
ALTER TABLE jobs.job ADD CONSTRAINT job_status_check
    CHECK (status IN ('queued', 'running', 'waiting', 'held', 'succeeded', 'partial', 'failed', 'cancelled'));
ALTER TABLE jobs.job ADD COLUMN waiting_on bigint REFERENCES core.llm_batch;
ALTER TABLE jobs.job ADD COLUMN wait_until timestamptz;
ALTER TABLE jobs.job ADD CONSTRAINT job_waiting_check CHECK ((status = 'waiting') = (waiting_on IS NOT NULL));

-- a run whose job waits on a batch waits too: nobody holds its lock, but it is not abandoned
ALTER TABLE checks.run DROP CONSTRAINT run_status_check;
ALTER TABLE checks.run ADD CONSTRAINT run_status_check
    CHECK (status IN ('running', 'waiting', 'succeeded', 'failed', 'stopped'));
ALTER TABLE checks.run DROP CONSTRAINT run_check;
ALTER TABLE checks.run ADD CONSTRAINT run_check CHECK ((status IN ('running', 'waiting')) = (finished_at IS NULL));
ALTER TABLE search.annotation_run DROP CONSTRAINT annotation_run_status_check;
ALTER TABLE search.annotation_run ADD CONSTRAINT annotation_run_status_check
    CHECK (status IN ('running', 'waiting', 'succeeded', 'failed', 'stopped'));

-- a judgement is published when its run has ended (finished_at), not while it waits on a batch
CREATE OR REPLACE VIEW checks.judgement_current AS
SELECT DISTINCT ON (j.trial_id, r.requirements)
       j.run_id, j.trial_id, j.label, j.parts, j.trial_record_id, j.document_versions, j.served_by, j.duration_ms,
       r.model, r.prompt, r.requirements, r.started_at,
       (EXISTS (SELECT FROM core.document_version v
                JOIN core.trial_document l ON l.document_id = v.document_id AND l.trial_id = j.trial_id
                WHERE v.id = ANY (j.document_versions) AND (v.replaced_at IS NOT NULL OR l.removed_at IS NOT NULL))
        OR EXISTS (SELECT FROM core.trial_document l JOIN core.document_current d ON d.id = l.document_id
                   WHERE l.trial_id = j.trial_id AND l.removed_at IS NULL
                     AND NOT (d.version_id = ANY (j.document_versions))
                     AND d.kind IN (SELECT jsonb_array_elements_text(r.requirements_text::jsonb -> 'document_kinds')))
        OR EXISTS (SELECT FROM core.trial_record_current c
                   WHERE c.trial_id = j.trial_id AND c.id IS DISTINCT FROM j.trial_record_id
                     AND c.fetched_at > r.started_at)
        -- OCR read a picture's text in one of its files after the check began: it read less than there is
        OR EXISTS (SELECT FROM core.document_version v JOIN core.page_ocr o ON o.file_sha256 = v.file_sha256
                   WHERE v.id = ANY (j.document_versions) AND o.text IS NOT NULL AND o.created_at > r.started_at)) AS outdated,
       r.finished_at AS published_at, j.agreement, j.withdrawn_at, j.withdrawn_reason
FROM checks.judgement j JOIN checks.run r ON r.id = j.run_id
WHERE j.error IS NULL AND r.finished_at IS NOT NULL
ORDER BY j.trial_id, r.requirements, j.run_id DESC;

-- migrate:down
SET LOCAL trialwatch.actor = '1';

CREATE OR REPLACE VIEW checks.judgement_current AS
SELECT DISTINCT ON (j.trial_id, r.requirements)
       j.run_id, j.trial_id, j.label, j.parts, j.trial_record_id, j.document_versions, j.served_by, j.duration_ms,
       r.model, r.prompt, r.requirements, r.started_at,
       (EXISTS (SELECT FROM core.document_version v
                JOIN core.trial_document l ON l.document_id = v.document_id AND l.trial_id = j.trial_id
                WHERE v.id = ANY (j.document_versions) AND (v.replaced_at IS NOT NULL OR l.removed_at IS NOT NULL))
        OR EXISTS (SELECT FROM core.trial_document l JOIN core.document_current d ON d.id = l.document_id
                   WHERE l.trial_id = j.trial_id AND l.removed_at IS NULL
                     AND NOT (d.version_id = ANY (j.document_versions))
                     AND d.kind IN (SELECT jsonb_array_elements_text(r.requirements_text::jsonb -> 'document_kinds')))
        OR EXISTS (SELECT FROM core.trial_record_current c
                   WHERE c.trial_id = j.trial_id AND c.id IS DISTINCT FROM j.trial_record_id
                     AND c.fetched_at > r.started_at)
        -- OCR read a picture's text in one of its files after the check began: it read less than there is
        OR EXISTS (SELECT FROM core.document_version v JOIN core.page_ocr o ON o.file_sha256 = v.file_sha256
                   WHERE v.id = ANY (j.document_versions) AND o.text IS NOT NULL AND o.created_at > r.started_at)) AS outdated,
       r.finished_at AS published_at, j.agreement, j.withdrawn_at, j.withdrawn_reason
FROM checks.judgement j JOIN checks.run r ON r.id = j.run_id
WHERE j.error IS NULL AND r.status <> 'running'
ORDER BY j.trial_id, r.requirements, j.run_id DESC;

UPDATE search.annotation_run SET status = 'failed', error = 'its batch was dropped', finished_at = clock_timestamp()
WHERE status = 'waiting';
ALTER TABLE search.annotation_run DROP CONSTRAINT annotation_run_status_check;
ALTER TABLE search.annotation_run ADD CONSTRAINT annotation_run_status_check
    CHECK (status IN ('running', 'succeeded', 'failed', 'stopped'));
UPDATE checks.run SET status = 'failed', error = 'its batch was dropped', finished_at = clock_timestamp()
WHERE status = 'waiting';
ALTER TABLE checks.run DROP CONSTRAINT run_check;
ALTER TABLE checks.run ADD CONSTRAINT run_check CHECK ((status = 'running') = (finished_at IS NULL));
ALTER TABLE checks.run DROP CONSTRAINT run_status_check;
ALTER TABLE checks.run ADD CONSTRAINT run_status_check CHECK (status IN ('running', 'succeeded', 'failed', 'stopped'));
UPDATE jobs.job SET status = 'queued', waiting_on = NULL, wait_until = NULL WHERE status = 'waiting';
ALTER TABLE jobs.job DROP CONSTRAINT job_waiting_check;
ALTER TABLE jobs.job DROP COLUMN wait_until;
ALTER TABLE jobs.job DROP COLUMN waiting_on;
ALTER TABLE jobs.job DROP CONSTRAINT job_status_check;
ALTER TABLE jobs.job ADD CONSTRAINT job_status_check
    CHECK (status IN ('queued', 'running', 'held', 'succeeded', 'partial', 'failed', 'cancelled'));
ALTER TABLE core.llm_call DROP COLUMN custom_id;
ALTER TABLE core.llm_call DROP COLUMN llm_batch_id;
ALTER TABLE core.llm_call DROP COLUMN tier;
DROP TABLE core.llm_batch_item;
DROP TABLE core.llm_batch;
