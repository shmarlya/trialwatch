"""Jobs: the workflow steps, queued in the database (jobs.job) and run one at a time by
`trialwatch worker` (worker.py), with retries (retry.py).

- sync: bring a source's list in (sources/stages.py sync_list)
- transfer: transfer some trials' evidence from a source (stages.transfer)
- check: judge trials with a check setup (checks/evidence.py run)
- refresh: the weekly chain of the three (refresh.py): sync every source, transfer what changed,
  check the trials whose evidence changed

A person starts sync, transfer and check jobs on the web interface; TrialWatch itself starts the
weekly refresh (jobs.schedule) and its child jobs, as the trialwatch actor. Each job runs as whoever
created it, so the audit log and the runs name them.
"""

KINDS = ("sync", "transfer", "check", "refresh")
# How many times a job is tried before it is left failed for a person (/admin/jobs)
MAX_ATTEMPTS = {"sync": 5, "transfer": 5, "check": 3, "refresh": 3}
# Lower runs first: a person's job goes ahead of the weekly refresh's batches
PRIORITY_PERSON = 50
PRIORITY_AUTOMATIC = 100
