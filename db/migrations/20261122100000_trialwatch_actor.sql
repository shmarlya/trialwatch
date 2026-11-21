-- migrate:up

-- One actor for everything no person does: TrialWatch itself. Actor 1 was 'migration'; it becomes
-- 'trialwatch', so migrations, seeding the first admin, scheduled jobs, automatic checks and automatic
-- publishing are all recorded as the same process. The older process actors (ctis-adapter,
-- evidence-check, admin-seed) stay, since the audit log names them, but nothing writes as them any more.
SET LOCAL trialwatch.actor = '1';

UPDATE core.actor SET name = 'trialwatch' WHERE id = 1 AND kind = 'process';
COMMENT ON TABLE core.actor IS 'a person, or a process; actor 1 (process trialwatch) is TrialWatch itself: '
    'migrations, scheduled jobs, automatic checks and publishing';


-- migrate:down
SET LOCAL trialwatch.actor = '1';

UPDATE core.actor SET name = 'migration' WHERE id = 1 AND kind = 'process';
COMMENT ON TABLE core.actor IS NULL;
