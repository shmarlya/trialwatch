"""The weekly refresh (a job of kind refresh, started by the weekly-refresh schedule as TrialWatch
itself): keep every trial's evidence and judgement current without anyone starting it.

Its stages, each a set of child jobs it waits for (jobs.job.status 'waiting'; the last child to end
queues it again for the next stage):

1. sync: one sync job per switched-on source that has a list
2. transfer: the trials each source lists that are new, changed since their transfer (stale) or whose
   last transfer failed, in batches of TRANSFER_BATCH, the ones the list calls recruiting first
3. annotate: the recruiting trials whose profile has no AI tags yet (new, or changed since), in batches
   of ANNOTATE_BATCH, as automatic jobs the monthly spending cap holds (patient search, ADR-008). A
   changed prompt does not tag every trial again: `trialwatch search annotate --all --outdated-prompt`.
4. check: the trials ready for a check that were never judged or whose judgement is outdated (a
   record or document it read changed), in batches of CHECK_BATCH, as automatic jobs (the monthly
   spending cap holds them). Nothing changed: no check, no money spent.
5. done: what happened, counted from the child jobs. A child that failed fails the refresh (it shows
   on /admin/jobs); the trials the other children brought in are still checked.
"""

import psycopg

from trialwatch.checks import requirements as requirement_sets
from trialwatch.checks import settings as check_settings
from trialwatch.db import checks as checks_db
from trialwatch.db import core
from trialwatch.db import jobs as jobs_db
from trialwatch.db import search as search_db
from trialwatch.db.jobs import Job
from trialwatch.jobs import MAX_ATTEMPTS, PRIORITY_AUTOMATIC
from trialwatch.jobs.steps import Context, Outcome
from trialwatch.sources import registry
from trialwatch.trials import readiness

TRANSFER_BATCH = 50
ANNOTATE_BATCH = 50
CHECK_BATCH = 20
# Which trials of a source the transfer stage takes (core.TRANSFER_CHOICES)
TRANSFER_WHICH = ("pending", "stale", "failed")
STAGES = ("sync", "transfer", "annotate", "check", "done")


def _sources(conn: psycopg.Connection, capability: str) -> list[core.RegisteredSource]:
    """The switched-on sources whose adapter can do this ("lists" or "transfers")."""
    found = []
    for source in core.registered_sources(conn):
        if not source.enabled or not source.adapter:
            continue
        adapter_class = registry.adapter_class(source.adapter)
        if getattr(adapter_class, capability):
            found.append(source)
    return found


def _add_child(conn: psycopg.Connection, job: Job, kind: str, params: dict) -> int:
    return jobs_db.enqueue(conn, kind, params, max_attempts=MAX_ATTEMPTS[kind], automatic=True, parent_id=job.id,
                           priority=PRIORITY_AUTOMATIC)


def _in_batches(items: list, size: int) -> list[list]:
    batches = []
    for start in range(0, len(items), size):
        batches.append(items[start:start + size])
    return batches


def _sync(ctx: Context, conn: psycopg.Connection, job: Job, result: dict) -> int:
    added = 0
    for source in _sources(conn, "lists"):
        _add_child(conn, job, "sync", {"source": source.code})
        added += 1
    ctx.add(f"sync: {added} sources")
    return added


def _transfer(ctx: Context, conn: psycopg.Connection, job: Job, result: dict) -> int:
    added = 0
    planned = {}
    for source in _sources(conn, "transfers"):
        # recruiting trials first (patient search, ADR-008): their batches are queued, so run, first
        keys = core.keys_to_transfer_any(conn, source.code, TRANSFER_WHICH)
        planned[source.code] = len(keys)
        for batch in _in_batches(keys, TRANSFER_BATCH):
            _add_child(conn, job, "transfer", {"source": source.code, "keys": batch})
            added += 1
        ctx.add(f"transfer: {len(keys)} trials from {source.code} ({', '.join(TRANSFER_WHICH)}) in "
                f"{len(_in_batches(keys, TRANSFER_BATCH))} jobs")
    result["to_transfer"] = planned
    return added


def _annotate(ctx: Context, conn: psycopg.Connection, job: Job, result: dict) -> int:
    waiting = search_db.trials_to_annotate(conn)
    result["to_annotate"] = len(waiting)
    added = 0
    for batch in _in_batches(waiting, ANNOTATE_BATCH):
        _add_child(conn, job, "annotate", {"trial_ids": batch})
        added += 1
    ctx.add(f"annotate: {len(waiting)} recruiting trials without AI tags for their profile, in {added} jobs")
    return added


def _check(ctx: Context, conn: psycopg.Connection, job: Job, result: dict) -> int:
    config = job.params.get("config", check_settings.DEFAULT)
    settings = check_settings.load(config)
    requirement_set = requirement_sets.load(settings.requirements)
    ready = readiness.ready_trials(conn, requirement_set.document_kinds)
    changed = checks_db.trials_needing_check(conn, ready, requirement_set.name)
    result["ready"] = len(ready)
    result["to_check"] = len(changed)
    added = 0
    for batch in _in_batches(changed, CHECK_BATCH):
        _add_child(conn, job, "check", {"trial_ids": batch, "config": config})
        added += 1
    ctx.add(f"check: {len(changed)} of {len(ready)} ready trials are new or changed, in {added} jobs")
    return added


def _done(ctx: Context, conn: psycopg.Connection, job: Job, result: dict) -> dict[str, str]:
    """Count the child jobs by kind and status; the failed ones are the refresh's failures."""
    counts = {}
    failed = {}
    for child in jobs_db.children(conn, job.id):
        key = f"{child.kind} {child.status}"
        if key not in counts:
            counts[key] = 0
        counts[key] += 1
        if child.status == "failed":
            failed[f"job {child.id} ({child.kind})"] = child.last_error
    result["children"] = counts
    listed = []
    for key, count in sorted(counts.items()):
        listed.append(f"{key} {count}")
    ctx.add(f"done: {', '.join(listed)}")
    return failed


PLANNERS = {"sync": _sync, "transfer": _transfer, "annotate": _annotate, "check": _check}


def run_refresh(ctx: Context, conn: psycopg.Connection, job: Job) -> Outcome:
    """Plan the next stage's child jobs and wait for them; a stage with nothing to do goes straight on."""
    result = dict(job.result)
    stage_done = result.get("stage")
    if stage_done == "done":
        # tried again after it ended (Retry on /admin/jobs): a new refresh from the start
        result = {}
        stage_done = None
    if stage_done is None:
        next_index = 0
    else:
        next_index = STAGES.index(stage_done) + 1
    while True:
        stage = STAGES[next_index]
        result["stage"] = stage
        if stage == "done":
            failed = _done(ctx, conn, job, result)
            if failed:
                result["failed_children"] = failed
            # a failed child already had its own attempts: the refresh itself is not tried again
            return Outcome(result, failed, final=True)
        if ctx.should_stop():
            return Outcome(result)
        added = PLANNERS[stage](ctx, conn, job, result)
        if added:
            return Outcome(result, {}, wait=True)
        next_index += 1

