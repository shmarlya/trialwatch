-- migrate:up

-- How far a running job is, for its page: items done of items to do in its current attempt (trials of a
-- transfer or a check), written with its heartbeat; when that attempt started, for the time it has taken
-- and an estimate of what is left. And what failed, item by item (a trial's key or name -> why), kept
-- with the job so its page can offer to look at or redo each one.
ALTER TABLE jobs.job
    ADD COLUMN progress_done integer CHECK (progress_done >= 0),
    ADD COLUMN progress_total integer CHECK (progress_total >= 0),
    ADD COLUMN attempt_started_at timestamp with time zone,
    ADD COLUMN failures jsonb NOT NULL DEFAULT '{}'::jsonb;

-- migrate:down

ALTER TABLE jobs.job
    DROP COLUMN failures,
    DROP COLUMN attempt_started_at,
    DROP COLUMN progress_total,
    DROP COLUMN progress_done;
