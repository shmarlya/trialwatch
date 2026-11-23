-- migrate:up

-- One row per model call (core.llm_call), the one place a call is kept: the request as sent (without
-- the system prompt, kept once with its run; attached files by their sha256), the answer as received,
-- how it ended, how long it took, and each billed attempt priced (more than one after a server-side
-- fallback). Every spend total is summed from it; a check's call step and an AI tagging name it.
--
-- It replaces core.api_call (one row per billed attempt) and the copies kept elsewhere: the request,
-- answer, tokens and cost in a check's call steps; a judgement's trace (its rules step holds it) and
-- tokens and cost; a check run's and a tagging run's totals; an annotation's request, answer, model,
-- tokens and cost. And checks.run.check_name, which nothing read.
SET LOCAL trialwatch.actor = '1';

CREATE TABLE core.llm_call (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at                 timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- who the call was made for: the person who started the check, or trialwatch
    actor_id           bigint NOT NULL REFERENCES core.actor,
    provider           text NOT NULL DEFAULT 'anthropic',
    -- what it was for: check, annotate
    operation          text NOT NULL,
    -- the model asked, and the one that answered (after a fallback, another)
    model              text NOT NULL,
    served_by          text,
    check_run_id       bigint REFERENCES checks.run,
    trial_id           bigint REFERENCES core.trial,
    -- its number within a trial's check (1: the first call, 2: widening)
    call_number        integer,
    annotation_run_id  bigint REFERENCES search.annotation_run,
    job_id             bigint REFERENCES jobs.job DEFAULT nullif(current_setting('trialwatch.job', true), '')::bigint,
    request            jsonb NOT NULL DEFAULT '{}',
    response           text,
    stop_reason        text,
    latency_ms         integer,
    -- why there is no usable answer: an API error, a refusal, running out of tokens
    error              text,
    -- the provider's request id, to match its own bills
    request_id         text,
    -- each billed attempt: {model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens,
    -- cost_usd (a decimal string; null: its model is not in the price list), prices (per million tokens)}
    attempts           jsonb NOT NULL DEFAULT '[]',
    -- the attempts' sums
    input_tokens       bigint NOT NULL DEFAULT 0,
    output_tokens      bigint NOT NULL DEFAULT 0,
    cache_write_tokens bigint NOT NULL DEFAULT 0,
    cache_read_tokens  bigint NOT NULL DEFAULT 0,
    -- what its priced attempts cost (NULL: none was priced), and how many were not priced
    cost_usd           numeric(14, 6),
    unpriced_attempts  integer NOT NULL DEFAULT 0
);
COMMENT ON TABLE core.llm_call IS 'every model call: the request as sent, the answer, how it ended, its billed '
    'attempts priced; the source of all spend totals';
CREATE INDEX llm_call_at_idx ON core.llm_call (at);
CREATE INDEX llm_call_run_idx ON core.llm_call (check_run_id, trial_id);
CREATE INDEX llm_call_annotation_idx ON core.llm_call (annotation_run_id, trial_id);
CREATE INDEX llm_call_job_idx ON core.llm_call (job_id);

-- each call's billed attempts, as core.api_call kept them
CREATE TEMPORARY TABLE billed ON COMMIT DROP AS
SELECT check_run_id, trial_id, call_number, annotation_run_id, min(at) AS at, min(actor_id) AS actor_id,
       min(operation) AS operation, min(job_id) AS job_id, (array_agg(model ORDER BY id))[1] AS model,
       min(request_id) AS request_id,
       jsonb_agg(jsonb_build_object('model', model, 'input_tokens', input_tokens, 'output_tokens', output_tokens,
                                    'cache_write_tokens', cache_write_tokens, 'cache_read_tokens', cache_read_tokens,
                                    'cost_usd', cost_usd::text, 'prices', prices) ORDER BY id) AS attempts,
       sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
       sum(cache_write_tokens) AS cache_write_tokens, sum(cache_read_tokens) AS cache_read_tokens,
       sum(cost_usd) AS cost_usd, count(*) FILTER (WHERE cost_usd IS NULL) AS unpriced
  FROM core.api_call
 GROUP BY check_run_id, trial_id, call_number, annotation_run_id;

-- a check's calls, from their trace steps
INSERT INTO core.llm_call (at, actor_id, operation, model, served_by, check_run_id, trial_id, call_number, job_id,
                           request, response, stop_reason, latency_ms, error, request_id, attempts, input_tokens,
                           output_tokens, cache_write_tokens, cache_read_tokens, cost_usd, unpriced_attempts)
SELECT s.at, r.actor_id, 'check', coalesce(s.data->>'model', r.model), s.data->>'served_by', s.run_id, s.trial_id,
       (s.data->>'call')::int, r.job_id, coalesce(s.data->'request', '{}'), s.data->>'answer', s.data->>'stop_reason',
       s.duration_ms, s.data->>'error', b.request_id, coalesce(b.attempts, '[]'), coalesce(b.input_tokens, 0),
       coalesce(b.output_tokens, 0), coalesce(b.cache_write_tokens, 0), coalesce(b.cache_read_tokens, 0), b.cost_usd,
       coalesce(b.unpriced, 0)
  FROM checks.step s
  JOIN checks.run r ON r.id = s.run_id
  LEFT JOIN billed b ON b.check_run_id = s.run_id AND b.trial_id = s.trial_id AND b.call_number = (s.data->>'call')::int
 WHERE s.name = 'call'
 ORDER BY s.at, s.run_id, s.trial_id, s.seq;

-- AI tagging's calls, from the annotations (an "invalid answer" was the tagging's error, not the call's)
INSERT INTO core.llm_call (at, actor_id, operation, model, served_by, trial_id, call_number, annotation_run_id, job_id,
                           request, response, latency_ms, error, request_id, attempts, input_tokens, output_tokens,
                           cache_write_tokens, cache_read_tokens, cost_usd, unpriced_attempts)
SELECT a.created_at, r.created_by, 'annotate', r.model, a.served_by, a.trial_id, 1, a.run_id, r.job_id,
       coalesce(a.request, '{}'), a.raw_answer, a.duration_ms,
       CASE WHEN a.error LIKE 'invalid answer:%' THEN NULL ELSE a.error END, b.request_id, coalesce(b.attempts, '[]'),
       coalesce(b.input_tokens, 0), coalesce(b.output_tokens, 0), coalesce(b.cache_write_tokens, 0),
       coalesce(b.cache_read_tokens, 0), b.cost_usd, coalesce(b.unpriced, 0)
  FROM search.annotation a
  JOIN search.annotation_run r ON r.id = a.run_id
  LEFT JOIN billed b ON b.annotation_run_id = a.run_id AND b.trial_id = a.trial_id
 ORDER BY a.id;

-- billed attempts of a call nothing else kept
INSERT INTO core.llm_call (at, actor_id, operation, model, check_run_id, trial_id, call_number, annotation_run_id,
                           job_id, request_id, attempts, input_tokens, output_tokens, cache_write_tokens,
                           cache_read_tokens, cost_usd, unpriced_attempts)
SELECT b.at, b.actor_id, b.operation, b.model, b.check_run_id, b.trial_id, b.call_number, b.annotation_run_id, b.job_id,
       b.request_id, b.attempts, b.input_tokens, b.output_tokens, b.cache_write_tokens, b.cache_read_tokens, b.cost_usd,
       b.unpriced
  FROM billed b
 WHERE NOT EXISTS (SELECT FROM core.llm_call c
                    WHERE c.check_run_id IS NOT DISTINCT FROM b.check_run_id AND c.trial_id IS NOT DISTINCT FROM b.trial_id
                      AND c.call_number IS NOT DISTINCT FROM b.call_number
                      AND c.annotation_run_id IS NOT DISTINCT FROM b.annotation_run_id)
 ORDER BY b.at;

-- a call step names its call, and keeps what was asked
UPDATE checks.step s
   SET data = (s.data - ARRAY['request', 'served_by', 'stop_reason', 'input_tokens', 'output_tokens',
                              'cache_write_tokens', 'cache_read_tokens', 'cost_usd', 'error', 'answer'])
              || jsonb_build_object('llm_call', c.id)
  FROM core.llm_call c
 WHERE s.name = 'call' AND c.operation = 'check' AND c.check_run_id = s.run_id AND c.trial_id = s.trial_id
   AND c.call_number = (s.data->>'call')::int;

DROP VIEW checks.judgement_current;
CREATE VIEW checks.judgement_current AS
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
                     AND c.fetched_at > r.started_at)) AS outdated,
       r.finished_at AS published_at, j.agreement, j.withdrawn_at, j.withdrawn_reason
FROM checks.judgement j JOIN checks.run r ON r.id = j.run_id
WHERE j.error IS NULL AND r.status <> 'running'
ORDER BY j.trial_id, r.requirements, j.run_id DESC;

ALTER TABLE checks.judgement DROP COLUMN trace, DROP COLUMN input_tokens, DROP COLUMN output_tokens,
    DROP COLUMN cache_write_tokens, DROP COLUMN cache_read_tokens, DROP COLUMN cost_usd;
ALTER TABLE checks.run DROP COLUMN check_name, DROP COLUMN input_tokens, DROP COLUMN output_tokens,
    DROP COLUMN cache_write_tokens, DROP COLUMN cache_read_tokens, DROP COLUMN cost_usd;
ALTER TABLE search.annotation DROP COLUMN served_by, DROP COLUMN request, DROP COLUMN raw_answer,
    DROP COLUMN input_tokens, DROP COLUMN output_tokens, DROP COLUMN cost_usd, DROP COLUMN duration_ms;
ALTER TABLE search.annotation_run DROP COLUMN input_tokens, DROP COLUMN output_tokens, DROP COLUMN cost_usd;
DROP TABLE core.api_call;


-- migrate:down
SET LOCAL trialwatch.actor = '1';

CREATE TABLE core.api_call (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at                 timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor_id           bigint NOT NULL REFERENCES core.actor,
    provider           text NOT NULL DEFAULT 'anthropic',
    model              text NOT NULL,
    operation          text NOT NULL,
    check_run_id       bigint REFERENCES checks.run,
    trial_id           bigint REFERENCES core.trial,
    call_number        integer,
    input_tokens       bigint NOT NULL DEFAULT 0,
    output_tokens      bigint NOT NULL DEFAULT 0,
    cache_write_tokens bigint NOT NULL DEFAULT 0,
    cache_read_tokens  bigint NOT NULL DEFAULT 0,
    cost_usd           numeric(14, 6),
    prices             jsonb,
    request_id         text,
    job_id             bigint REFERENCES jobs.job DEFAULT nullif(current_setting('trialwatch.job', true), '')::bigint,
    annotation_run_id  bigint REFERENCES search.annotation_run
);
COMMENT ON TABLE core.api_call IS 'every billed call to a paid API, with its tokens and cost: the source of all spend totals';
CREATE INDEX api_call_at_idx ON core.api_call (at);
CREATE INDEX api_call_run_idx ON core.api_call (check_run_id, trial_id);
CREATE INDEX api_call_job_idx ON core.api_call (job_id);
INSERT INTO core.api_call (at, actor_id, provider, model, operation, check_run_id, trial_id, call_number, input_tokens,
                           output_tokens, cache_write_tokens, cache_read_tokens, cost_usd, prices, request_id, job_id,
                           annotation_run_id)
SELECT c.at, c.actor_id, c.provider, a->>'model', c.operation, c.check_run_id, c.trial_id, c.call_number,
       (a->>'input_tokens')::bigint, (a->>'output_tokens')::bigint, (a->>'cache_write_tokens')::bigint,
       (a->>'cache_read_tokens')::bigint, (a->>'cost_usd')::numeric, a->'prices', c.request_id, c.job_id,
       c.annotation_run_id
  FROM core.llm_call c, jsonb_array_elements(c.attempts) WITH ORDINALITY AS t(a, n)
 ORDER BY c.id, t.n;

ALTER TABLE search.annotation_run ADD COLUMN input_tokens bigint NOT NULL DEFAULT 0,
    ADD COLUMN output_tokens bigint NOT NULL DEFAULT 0, ADD COLUMN cost_usd numeric(14, 6) NOT NULL DEFAULT 0;
UPDATE search.annotation_run r SET
    input_tokens = (SELECT coalesce(sum(c.input_tokens), 0) FROM core.llm_call c WHERE c.annotation_run_id = r.id),
    output_tokens = (SELECT coalesce(sum(c.output_tokens), 0) FROM core.llm_call c WHERE c.annotation_run_id = r.id),
    cost_usd = (SELECT coalesce(sum(c.cost_usd), 0) FROM core.llm_call c WHERE c.annotation_run_id = r.id);
ALTER TABLE search.annotation ADD COLUMN served_by text, ADD COLUMN request jsonb, ADD COLUMN raw_answer text,
    ADD COLUMN input_tokens integer NOT NULL DEFAULT 0, ADD COLUMN output_tokens integer NOT NULL DEFAULT 0,
    ADD COLUMN cost_usd numeric(14, 6), ADD COLUMN duration_ms integer;
UPDATE search.annotation a SET served_by = c.served_by, request = c.request, raw_answer = c.response,
       input_tokens = c.input_tokens, output_tokens = c.output_tokens, cost_usd = c.cost_usd, duration_ms = c.latency_ms
  FROM core.llm_call c WHERE c.annotation_run_id = a.run_id AND c.trial_id = a.trial_id;

ALTER TABLE checks.run ADD COLUMN check_name text NOT NULL DEFAULT 'evidence',
    ADD COLUMN input_tokens bigint NOT NULL DEFAULT 0, ADD COLUMN output_tokens bigint NOT NULL DEFAULT 0,
    ADD COLUMN cache_write_tokens bigint NOT NULL DEFAULT 0, ADD COLUMN cache_read_tokens bigint NOT NULL DEFAULT 0,
    ADD COLUMN cost_usd numeric(14, 6) NOT NULL DEFAULT 0;
ALTER TABLE checks.run ALTER COLUMN check_name DROP DEFAULT;
UPDATE checks.run r SET
    input_tokens = (SELECT coalesce(sum(c.input_tokens), 0) FROM core.llm_call c WHERE c.check_run_id = r.id),
    output_tokens = (SELECT coalesce(sum(c.output_tokens), 0) FROM core.llm_call c WHERE c.check_run_id = r.id),
    cache_write_tokens = (SELECT coalesce(sum(c.cache_write_tokens), 0) FROM core.llm_call c WHERE c.check_run_id = r.id),
    cache_read_tokens = (SELECT coalesce(sum(c.cache_read_tokens), 0) FROM core.llm_call c WHERE c.check_run_id = r.id),
    cost_usd = (SELECT coalesce(sum(c.cost_usd), 0) FROM core.llm_call c WHERE c.check_run_id = r.id);

ALTER TABLE checks.judgement ADD COLUMN trace jsonb, ADD COLUMN input_tokens integer, ADD COLUMN output_tokens integer,
    ADD COLUMN cache_write_tokens integer, ADD COLUMN cache_read_tokens integer, ADD COLUMN cost_usd numeric(14, 6);
UPDATE checks.judgement j SET
    trace = (SELECT nullif(s.data->'trace', '{}') FROM checks.step s
              WHERE s.run_id = j.run_id AND s.trial_id = j.trial_id AND s.name = 'rules'),
    input_tokens = t.input_tokens, output_tokens = t.output_tokens, cache_write_tokens = t.cache_write_tokens,
    cache_read_tokens = t.cache_read_tokens,
    cost_usd = CASE WHEN t.unpriced > 0 THEN NULL ELSE t.cost_usd END
  FROM (SELECT check_run_id, trial_id, sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
               sum(cache_write_tokens) AS cache_write_tokens, sum(cache_read_tokens) AS cache_read_tokens,
               coalesce(sum(cost_usd), 0) AS cost_usd, sum(unpriced_attempts) AS unpriced
          FROM core.llm_call WHERE check_run_id IS NOT NULL GROUP BY check_run_id, trial_id) t
 WHERE t.check_run_id = j.run_id AND t.trial_id = j.trial_id;

UPDATE checks.step s
   SET data = (s.data - 'llm_call')
              || jsonb_build_object('request', c.request, 'served_by', c.served_by, 'stop_reason', c.stop_reason,
                                    'input_tokens', c.input_tokens, 'output_tokens', c.output_tokens,
                                    'cache_write_tokens', c.cache_write_tokens, 'cache_read_tokens', c.cache_read_tokens,
                                    'cost_usd', CASE WHEN c.unpriced_attempts > 0 THEN NULL ELSE c.cost_usd::text END,
                                    'error', c.error, 'answer', c.response)
  FROM core.llm_call c
 WHERE s.name = 'call' AND c.id = (s.data->>'llm_call')::bigint;

DROP VIEW checks.judgement_current;
CREATE VIEW checks.judgement_current AS
SELECT DISTINCT ON (j.trial_id, r.requirements)
       j.run_id, j.trial_id, j.label, j.parts, j.trace, j.trial_record_id, j.document_versions, j.served_by,
       j.input_tokens, j.output_tokens, j.duration_ms, r.model, r.prompt, r.requirements, r.started_at,
       (EXISTS (SELECT FROM core.document_version v
                JOIN core.trial_document l ON l.document_id = v.document_id AND l.trial_id = j.trial_id
                WHERE v.id = ANY (j.document_versions) AND (v.replaced_at IS NOT NULL OR l.removed_at IS NOT NULL))
        OR EXISTS (SELECT FROM core.trial_document l JOIN core.document_current d ON d.id = l.document_id
                   WHERE l.trial_id = j.trial_id AND l.removed_at IS NULL
                     AND NOT (d.version_id = ANY (j.document_versions))
                     AND d.kind IN (SELECT jsonb_array_elements_text(r.requirements_text::jsonb -> 'document_kinds')))
        OR EXISTS (SELECT FROM core.trial_record_current c
                   WHERE c.trial_id = j.trial_id AND c.id IS DISTINCT FROM j.trial_record_id
                     AND c.fetched_at > r.started_at)) AS outdated,
       r.finished_at AS published_at, j.agreement, j.withdrawn_at, j.withdrawn_reason
FROM checks.judgement j JOIN checks.run r ON r.id = j.run_id
WHERE j.error IS NULL AND r.status <> 'running'
ORDER BY j.trial_id, r.requirements, j.run_id DESC;

DROP TABLE core.llm_call;
