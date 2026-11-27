-- migrate:up

-- A job that ran to its end but left some items undone (a trial not transferred, a check that failed)
-- ends `partial`, not `failed`: `failed` is kept for a job that could not run as a whole (a crash, a
-- database or network error, a source whose list could not be read, an invalid setup). jobs/worker.py.
SET LOCAL trialwatch.actor = '1';

ALTER TABLE jobs.job DROP CONSTRAINT job_status_check;
-- queued: waits for a worker (from run_after); running; held: by the spending cap; succeeded / partial /
-- failed / cancelled: ended
ALTER TABLE jobs.job ADD CONSTRAINT job_status_check
    CHECK (status IN ('queued', 'running', 'held', 'succeeded', 'partial', 'failed', 'cancelled'));
ALTER TABLE jobs.job DROP CONSTRAINT job_check;
ALTER TABLE jobs.job ADD CONSTRAINT job_check
    CHECK ((status IN ('succeeded', 'partial', 'failed', 'cancelled')) = (finished_at IS NOT NULL));

-- migrate:down
SET LOCAL trialwatch.actor = '1';

UPDATE jobs.job SET status = 'failed' WHERE status = 'partial';
ALTER TABLE jobs.job DROP CONSTRAINT job_check;
ALTER TABLE jobs.job ADD CONSTRAINT job_check
    CHECK ((status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL));
ALTER TABLE jobs.job DROP CONSTRAINT job_status_check;
ALTER TABLE jobs.job ADD CONSTRAINT job_status_check
    CHECK (status IN ('queued', 'running', 'held', 'succeeded', 'failed', 'cancelled'));
