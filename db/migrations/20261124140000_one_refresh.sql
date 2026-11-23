-- migrate:up

-- A refresh is one job that runs its steps one after the other (sync, transfer, annotate, check;
-- jobs/refresh.py), so no job has child jobs or waits for them any more. A refresh still waiting for
-- its child jobs is cancelled; its children run as jobs of their own, and the next refresh does the rest.
SET LOCAL trialwatch.actor = '1';

UPDATE jobs.job SET status = 'cancelled', cancel_requested = true, finished_at = clock_timestamp(),
       last_error = 'cancelled: a refresh no longer waits for child jobs'
 WHERE status = 'waiting';

-- its index (job_parent_idx) goes with it
ALTER TABLE jobs.job DROP COLUMN parent_id;
ALTER TABLE jobs.job DROP CONSTRAINT job_status_check;
-- queued: waits for a worker (from run_after); running; held: by the spending cap; succeeded / failed /
-- cancelled: ended
ALTER TABLE jobs.job ADD CONSTRAINT job_status_check
    CHECK (status IN ('queued', 'running', 'held', 'succeeded', 'failed', 'cancelled'));

-- migrate:down
SET LOCAL trialwatch.actor = '1';

ALTER TABLE jobs.job DROP CONSTRAINT job_status_check;
ALTER TABLE jobs.job ADD CONSTRAINT job_status_check
    CHECK (status IN ('queued', 'running', 'waiting', 'held', 'succeeded', 'failed', 'cancelled'));
ALTER TABLE jobs.job ADD COLUMN parent_id bigint REFERENCES jobs.job;
CREATE INDEX job_parent_idx ON jobs.job (parent_id);
