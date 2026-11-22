-- migrate:up

-- How often a schedule runs: daily (at its hour), weekly (on its weekday, at its hour) or monthly (on
-- the 1st, at its hour). Schedules start switched off: an admin switches one on at /admin/jobs, so a
-- fresh database never starts paid checks by itself.
SET LOCAL trialwatch.actor = '1';

ALTER TABLE jobs.schedule
    ADD COLUMN frequency text NOT NULL DEFAULT 'weekly' CHECK (frequency IN ('daily', 'weekly', 'monthly')),
    ALTER COLUMN enabled SET DEFAULT false;
COMMENT ON COLUMN jobs.schedule.weekday IS 'ISO day of the week (1 Monday .. 7 Sunday); used when weekly';
COMMENT ON COLUMN jobs.schedule.frequency IS 'daily | weekly | monthly (on the 1st)';

UPDATE jobs.schedule SET enabled = false;


-- migrate:down
SET LOCAL trialwatch.actor = '1';

ALTER TABLE jobs.schedule DROP COLUMN frequency, ALTER COLUMN enabled SET DEFAULT true;
COMMENT ON COLUMN jobs.schedule.weekday IS NULL;
