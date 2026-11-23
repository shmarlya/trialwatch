"""Jobs: the workflow steps, queued in the database (jobs.job) and run one at a time by
`trialwatch worker` (worker.py), with retries (retry.py).

- sync: bring a source's list in (trials/ingest.py sync_list)
- transfer: transfer some trials' evidence from a source (ingest.transfer)
- check: judge trials with a check setup (checks/evidence.py run)
- annotate: tag recruiting trials for patient search with AI, each tag with a checked quote
  (search/annotate.py, ADR-008)
- refresh: one job (refresh.py) that syncs every source, transfers what changed, tags the recruiting
  trials whose profile changed and checks the trials whose evidence changed, step after step

Each kind's label, attempts, spending cap and params check are in tasks.py. A person starts sync,
transfer and check jobs on the web interface; TrialWatch itself starts the refresh (jobs.schedule), as
the trialwatch actor. Each job runs as whoever created it, so the audit log and the runs name them.
"""

# Lower runs first: a person's job goes ahead of the refresh
PRIORITY_PERSON = 50
PRIORITY_AUTOMATIC = 100
