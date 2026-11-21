-- migrate:up

-- Jobs: the workflow steps (sync a source's list, transfer evidence, check trials) and the weekly
-- refresh that chains them, queued in the database and run by `trialwatch worker`. A job survives a
-- restart; a failed attempt is tried again later (src/trialwatch/jobs/retry.py says how often and when);
-- a job that ran out of attempts stays failed until someone retries it (/admin/jobs).
SET LOCAL trialwatch.actor = '1';

CREATE SCHEMA jobs;

-- When a job is due by itself: the weekly refresh (src/trialwatch/jobs/refresh.py)
CREATE TABLE jobs.schedule (
    id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    kind        text NOT NULL CHECK (kind IN ('sync', 'transfer', 'check', 'refresh')),
    params      jsonb NOT NULL DEFAULT '{}',
    -- ISO day of the week (1 Monday .. 7 Sunday) and hour, UTC
    weekday     smallint NOT NULL CHECK (weekday BETWEEN 1 AND 7),
    hour_utc    smallint NOT NULL CHECK (hour_utc BETWEEN 0 AND 23),
    enabled     boolean NOT NULL DEFAULT true,
    next_run_at timestamptz NOT NULL
);
CREATE TRIGGER schedule_audit AFTER INSERT OR DELETE OR UPDATE ON jobs.schedule
    FOR EACH ROW EXECUTE FUNCTION core.audit('id');

CREATE TABLE jobs.job (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind             text NOT NULL CHECK (kind IN ('sync', 'transfer', 'check', 'refresh')),
    -- what to do, e.g. {"source": "ctis", "keys": [...]} or {"trial_ids": [...], "config": "default"}
    params           jsonb NOT NULL DEFAULT '{}',
    -- started by TrialWatch itself (a schedule, a refresh), not a person: the monthly spending cap
    -- holds automatic checks
    automatic        boolean NOT NULL DEFAULT false,
    parent_id        bigint REFERENCES jobs.job,
    schedule_id      integer REFERENCES jobs.schedule,
    -- lower runs first
    priority         smallint NOT NULL DEFAULT 100,
    -- queued: waits for a worker (from run_after); running; waiting: for its child jobs; held: by the
    -- spending cap; succeeded / failed / cancelled: ended
    status           text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'waiting', 'held', 'succeeded', 'failed', 'cancelled')),
    attempts         integer NOT NULL DEFAULT 0,
    max_attempts     integer NOT NULL CHECK (max_attempts >= 1),
    run_after        timestamptz NOT NULL DEFAULT clock_timestamp(),
    cancel_requested boolean NOT NULL DEFAULT false,
    last_error       text,
    -- what it did, e.g. counts, the runs it made, what failed
    result           jsonb NOT NULL DEFAULT '{}',
    -- the worker running it, and when it last said it is alive
    locked_by        text,
    heartbeat_at     timestamptz,
    created_by       bigint NOT NULL REFERENCES core.actor,
    created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at       timestamptz,
    finished_at      timestamptz,
    CHECK ((status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL))
);
CREATE INDEX job_queue_idx ON jobs.job (priority, id) WHERE status = 'queued';
CREATE INDEX job_status_idx ON jobs.job (status, id DESC);
CREATE INDEX job_parent_idx ON jobs.job (parent_id);
-- a heartbeat alone is not worth an audit row
CREATE TRIGGER job_audit AFTER INSERT OR DELETE OR UPDATE ON jobs.job
    FOR EACH ROW EXECUTE FUNCTION core.audit('id', 'heartbeat_at');

-- The progress lines of a job, per attempt, as its page shows them while it runs
CREATE TABLE jobs.job_line (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id  bigint NOT NULL REFERENCES jobs.job ON DELETE CASCADE,
    attempt integer NOT NULL,
    at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    line    text NOT NULL
);
CREATE INDEX job_line_job_idx ON jobs.job_line (job_id, id);

-- The job a run or a billed call was made for. The worker names it for its session
-- (trialwatch.job), so the code that starts runs and records calls never has to pass it on.
ALTER TABLE checks.run ADD COLUMN job_id bigint
    DEFAULT nullif(current_setting('trialwatch.job', true), '')::bigint REFERENCES jobs.job;
ALTER TABLE core.ingest_run ADD COLUMN job_id bigint
    DEFAULT nullif(current_setting('trialwatch.job', true), '')::bigint REFERENCES jobs.job;
ALTER TABLE core.api_call ADD COLUMN job_id bigint
    DEFAULT nullif(current_setting('trialwatch.job', true), '')::bigint REFERENCES jobs.job;
CREATE INDEX run_job_idx ON checks.run (job_id);
CREATE INDEX ingest_run_job_idx ON core.ingest_run (job_id);
CREATE INDEX api_call_job_idx ON core.api_call (job_id);

-- Every Monday at 03:00 UTC: sync every source's list, transfer what changed, check what changed
INSERT INTO jobs.schedule (name, kind, weekday, hour_utc, next_run_at)
VALUES ('weekly-refresh', 'refresh', 1, 3,
        date_trunc('week', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' + interval '7 days 3 hours');


-- migrate:down
SET LOCAL trialwatch.actor = '1';

ALTER TABLE core.api_call DROP COLUMN job_id;
ALTER TABLE core.ingest_run DROP COLUMN job_id;
ALTER TABLE checks.run DROP COLUMN job_id;
DROP SCHEMA jobs CASCADE;
