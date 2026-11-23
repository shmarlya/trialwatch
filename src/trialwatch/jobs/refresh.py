"""The refresh (a job of kind refresh, started by the refresh schedule as TrialWatch itself): keeps
every trial's evidence, AI tags and judgement current without anyone starting it. One job, step after
step, each the same step a job of its own kind runs (jobs/steps.py):

1. sync every switched-on source that has a list
2. transfer the trials each source lists that are new, changed since their transfer (stale) or whose
   last transfer failed, the ones the list calls recruiting first (patient search, ADR-008)
3. annotate the recruiting trials whose profile has no AI tags yet (new, or changed since). A changed
   prompt does not tag every trial again: `trialwatch search annotate --all --outdated-prompt`.
4. check the trials ready for a check that were never judged or whose judgement is outdated (a record
   or document it read changed). Nothing changed: no check, no money spent.

Steps 3 and 4 call a paid model: the monthly spending cap holds the refresh before them, and it carries
on when the cap allows. A step that fails does not stop the others: its failure fails the attempt, and
the refresh is tried again later. Each step picks its work from what is stored now, so a new attempt
carries on where the last one stopped (a transferred trial is no longer stale, a tagged or judged one
no longer waits), and a source synced in an earlier attempt is not listed again.
"""

import traceback
from collections.abc import Callable

import psycopg

from trialwatch.checks import requirements as requirement_sets
from trialwatch.checks import settings as check_settings
from trialwatch.db import checks as checks_db
from trialwatch.db import core
from trialwatch.jobs import steps
from trialwatch.jobs.retry import PermanentError
from trialwatch.jobs.steps import Context, Outcome
from trialwatch.sources import registry
from trialwatch.trials import readiness

# Which trials of a source it transfers (core.TRANSFER_CHOICES)
TRANSFER_WHICH = ("pending", "stale", "failed")

Step = Callable[[Context, psycopg.Connection, dict], Outcome]


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


def _transfer(ctx: Context, conn: psycopg.Connection, params: dict) -> Outcome:
    source = params["source"]
    # recruiting trials first (patient search, ADR-008)
    keys = core.keys_to_transfer_any(conn, source, TRANSFER_WHICH)
    ctx.add(f"{len(keys)} trials of {source} are {' or '.join(TRANSFER_WHICH)}")
    return steps.run_transfer(ctx, conn, {"source": source, "keys": keys})


def _check(ctx: Context, conn: psycopg.Connection, params: dict) -> Outcome:
    config = params["config"]
    settings = check_settings.load(config)
    requirement_set = requirement_sets.load(settings.requirements)
    ready = readiness.ready_trials(conn, requirement_set.document_kinds)
    changed = checks_db.trials_needing_check(conn, ready, requirement_set.name)
    ctx.add(f"{len(changed)} of {len(ready)} trials ready for a check are new or changed")
    outcome = steps.run_check(ctx, conn, {"trial_ids": changed, "config": config})
    outcome.result["ready"] = len(ready)
    return outcome


def _run(ctx: Context, conn: psycopg.Connection, name: str, step: Step, params: dict, result: dict,
         failed: dict[str, str]) -> bool:
    """Run one step, as the refresh (its runs name the refresh's job, its retries are the refresh's).
    What failed in it is one of the refresh's failures, not the end of the refresh. Its result goes in
    the refresh's under `name`; True when nothing in it failed."""
    ctx.add(f"— {name}")
    try:
        outcome = step(ctx, conn, params)
    except PermanentError as exc:
        error = str(exc)
    except Exception as exc:  # a network error, a busy source: tried again with the refresh
        error = f"{type(exc).__name__}: {exc}"
        ctx.add(traceback.format_exc())
    else:
        result[name] = outcome.result
        for item, item_error in outcome.failed.items():
            failed[f"{name}, {item}"] = item_error
        return not outcome.failed
    ctx.add(f"{name} failed: {error}")
    failed[name] = error
    return False


def run_refresh(ctx: Context, conn: psycopg.Connection, params: dict) -> Outcome:
    result = {}
    failed: dict[str, str] = {}
    # the sources an earlier attempt of this refresh synced without a failure
    earlier = {}
    if ctx.job is not None:
        earlier = ctx.job.result
    synced = list(earlier.get("synced", []))
    result["synced"] = synced
    for source in _sources(conn, "lists"):
        if ctx.should_stop():
            return Outcome(result, failed)
        name = f"sync {source.code}"
        if source.code in synced:
            ctx.add(f"— {name}: synced in an earlier attempt")
            continue
        if _run(ctx, conn, name, steps.run_sync, {"source": source.code}, result, failed):
            synced.append(source.code)
    for source in _sources(conn, "transfers"):
        if ctx.should_stop():
            return Outcome(result, failed)
        _run(ctx, conn, f"transfer {source.code}", _transfer, {"source": source.code}, result, failed)
    # the steps that call a paid model: an automatic refresh is held by the spending cap from here on
    ctx.watch_cap = ctx.automatic
    if ctx.should_stop():
        return Outcome(result, failed)
    _run(ctx, conn, "annotate", steps.run_annotate, {"every": True}, result, failed)
    if ctx.should_stop():
        return Outcome(result, failed)
    config = params.get("config", check_settings.DEFAULT)
    _run(ctx, conn, "check", _check, {"config": config}, result, failed)
    return Outcome(result, failed)
